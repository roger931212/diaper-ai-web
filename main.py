import os
import json
import uuid
import glob
import logging
import base64
from datetime import datetime

from fastapi import (
    FastAPI,
    UploadFile,
    File,
    Form,
    Request,
    Header,
    HTTPException,
    Depends,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# ============================
# Log 設定
# ============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

# ============================
# 1) 設定與環境變數檢查
# ============================

INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "").strip()

# ✅ 安全性強制：若未設定 Key，拒絕啟動伺服器
if not INTERNAL_API_KEY:
    logger.critical("⚠️ 尚未設定 INTERNAL_API_KEY！為了安全，伺服器拒絕啟動。")
    raise RuntimeError("INTERNAL_API_KEY must be set in environment variables.")

# （可選）上傳檔案大小上限：預設 6MB（你可自行調整）
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", "6291456"))  # 6 * 1024 * 1024

# （可選）claim 回傳圖片大小上限：避免 base64 response 太大（預設同 MAX_UPLOAD_BYTES）
MAX_CLAIM_IMAGE_BYTES = int(os.getenv("MAX_CLAIM_IMAGE_BYTES", str(MAX_UPLOAD_BYTES)))

# 資料夾結構定義
# pending: 用戶剛上傳，等待內網抓取
# processing: 內網正在抓取中（鎖定狀態），等待 Confirm 刪除
# stubs: 僅存狀態與結果，不含個資，永久保留
DIRS = {
    "uploads": "storage/uploads",       # 原始圖片 (Confirm 後刪除)
    "pending": "storage/pending",       # 完整案件 JSON (Confirm 後刪除)
    "processing": "storage/processing", # 處理中案件 JSON (Confirm 後刪除)
    "stubs": "storage/stubs",           # 存根 JSON (保留)
}

# 初始化資料夾
for _, path in DIRS.items():
    os.makedirs(path, exist_ok=True)

templates = Jinja2Templates(directory="templates")


# ============================
# 2) 內網 API Key 驗證 (Dependency)
# ============================
def verify_internal_key(x_api_key: str = Header(..., alias="X-API-KEY")):
    """
    驗證內網請求的 API Key。
    使用 Depends 注入，若驗證失敗直接拋出 401。
    """
    if x_api_key != INTERNAL_API_KEY:
        logger.warning("Invalid API Key attempt.")
        raise HTTPException(status_code=401, detail="Unauthorized")
    return x_api_key


# ============================
# 3) 工具函式
# ============================
def guess_ext(filename: str, content_type: str) -> str:
    """推測副檔名，預設為 .jpg（外網不顯示照片，只供內網使用）"""
    filename = (filename or "").lower()
    content_type = (content_type or "").lower()

    if filename.endswith(".png") or content_type == "image/png":
        return ".png"
    if filename.endswith(".jpg") or filename.endswith(".jpeg") or content_type == "image/jpeg":
        return ".jpg"
    return ".jpg"


def save_json(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def load_json(path: str) -> dict:
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Data not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def safe_remove(path: str):
    """安全刪除檔案，忽略不存在的錯誤"""
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning(f"Failed to remove {path}: {e}")


def stub_path(case_id: str) -> str:
    return os.path.join(DIRS["stubs"], f"{case_id}.json")


def pending_path(case_id: str) -> str:
    return os.path.join(DIRS["pending"], f"{case_id}.json")


def processing_path(case_id: str) -> str:
    return os.path.join(DIRS["processing"], f"{case_id}.json")


def update_stub_status(case_id: str, **fields):
    """更新對外顯示的 Stub 狀態"""
    sp = stub_path(case_id)
    if not os.path.exists(sp):
        return
    try:
        stub = load_json(sp)
        stub.update(fields)
        save_json(sp, stub)
    except Exception as e:
        logger.error(f"Failed to update stub {case_id}: {e}")


def _validate_case_id(case_id: str):
    """避免奇怪字串造成路徑問題：強制必須是 UUID"""
    try:
        uuid.UUID(case_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid case_id")


# ============================
# 4) Routes（外網：給家屬使用）
# ============================

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <h1>Diaper Rash AI Service (External)</h1>
    <p>System Status: <span style='color:green'>Online</span></p>
    <p>Go to <a href='/form'>/form</a> to submit a case.</p>
    """


@app.get("/form", response_class=HTMLResponse)
async def form_page(request: Request):
    return templates.TemplateResponse("form.html", {"request": request})


@app.post("/submit_case")
def submit_case(
    name: str = Form(...),
    phone: str = Form(...),
    line_id: str = Form(...),
    image: UploadFile = File(...),
):
    """
    [外網] 接收表單
    - 暫存圖片：storage/uploads
    - 暫存完整 JSON（含個資）：storage/pending
    - 建立 stub（不含個資）：storage/stubs
    """
    case_id = str(uuid.uuid4())
    receipt = uuid.uuid4().hex  # 對帳 Token
    created_at = datetime.now().isoformat(timespec="seconds")

    # 1) 儲存圖片（chunk write + 限制大小）
    ext = guess_ext(image.filename, image.content_type)
    img_filename = f"{case_id}{ext}"
    img_path = os.path.join(DIRS["uploads"], img_filename)

    total = 0
    try:
        with open(img_path, "wb") as f:
            while True:
                chunk = image.file.read(1024 * 1024)  # 1MB
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="File too large")
                f.write(chunk)
    except HTTPException:
        safe_remove(img_path)
        raise
    except Exception as e:
        safe_remove(img_path)
        logger.error(f"Image save failed: {e}")
        raise HTTPException(status_code=500, detail="File upload failed")
    finally:
        try:
            image.file.close()
        except Exception:
            pass

    # 2) 完整案件（含個資）→ pending
    record = {
        "id": case_id,
        "receipt": receipt,
        "created_at": created_at,
        "name": name,
        "phone": phone,
        "line_id": line_id,
        "image_filename": img_filename,
        "status": "pending",
    }
    save_json(pending_path(case_id), record)

    # 3) stub（不含個資）→ 給結果頁查詢 + 對帳
    # 注意：result.html 不建議顯示 receipt（避免不必要外洩）
    stub = {
        "id": case_id,
        "receipt": receipt,
        "created_at": created_at,
        "status": "pending",
        "ai_level": None,
        "ai_prob": None,
        "ai_suggestion": None,
    }
    save_json(stub_path(case_id), stub)

    logger.info(f"New case submitted: {case_id}")
    return RedirectResponse(url=f"/result/{case_id}", status_code=302)


@app.get("/result/{case_id}", response_class=HTMLResponse)
def result_page(request: Request, case_id: str):
    """
    [外網] 結果頁
    - 只讀取 stub（不含個資、不顯示照片）
    """
    _validate_case_id(case_id)

    sp = stub_path(case_id)
    if not os.path.exists(sp):
        return templates.TemplateResponse(
            "error.html", {"request": request, "message": "Case not found"}
        )

    stub = load_json(sp)
    return templates.TemplateResponse("result.html", {"request": request, "case": stub})


# ==========================================
# 5) 內網專用 API（Two-Phase Commit 流程）
#    Step 1: /claim_case        -> 鎖定案件 (Pending -> Processing)
#    Step 2: /confirm_case      -> 刪除個資 (Processing -> Delete)
#    Step 3: /update_ai_result  -> 更新 stub 結果
# ==========================================

@app.post("/claim_case")
def claim_case(x_api_key: str = Depends(verify_internal_key)):
    """
    Step 1: 內網請求案件
    - 使用 os.rename 進行原子操作，避免多個 worker 搶同一筆
    - 回傳完整資料與圖片 base64（內網拿去存 DB/檔案）
    - 不刪除：要等 confirm_case 才 purge（避免內網保存失敗導致資料遺失）
    """
    pending_files = glob.glob(os.path.join(DIRS["pending"], "*.json"))
    if not pending_files:
        return {"status": "empty"}

    # FIFO：用 mtime 排序
    pending_files.sort(key=lambda p: os.path.getmtime(p))

    # ✅ 多筆重試：避免只卡在第一筆 conflict
    for _ in range(min(10, len(pending_files))):
        target_file = pending_files[0]
        pending_files = pending_files[1:]

        filename = os.path.basename(target_file)
        case_id = os.path.splitext(filename)[0]

        # 基本保護（避免奇怪檔名）
        try:
            uuid.UUID(case_id)
        except Exception:
            logger.warning(f"Skipping non-uuid file in pending: {filename}")
            continue

        dest_path = processing_path(case_id)

        try:
            os.rename(target_file, dest_path)
        except OSError:
            # 被其他 worker 搶走，換下一筆
            continue

        # --- 成功搶到案件（已在 processing） ---
        try:
            rec = load_json(dest_path)

            img_path = os.path.join(DIRS["uploads"], rec.get("image_filename", ""))

            if not os.path.exists(img_path):
                logger.error(f"Image missing for case {case_id}")
                update_stub_status(case_id, status="error_missing_image", updated_at=datetime.now().isoformat(timespec="seconds"))

                # ✅ 把案件移回 pending（讓之後可重試/人工處理）
                try:
                    os.rename(dest_path, pending_path(case_id))
                except Exception as e:
                    logger.error(f"Failed to move case back to pending: {e}")

                return {"status": "error", "message": "Image file missing"}

            # ✅ 限制 claim 回傳圖片大小（避免 response 太大）
            img_size = os.path.getsize(img_path)
            if img_size > MAX_CLAIM_IMAGE_BYTES:
                logger.error(f"Image too large to claim (size={img_size}) case={case_id}")
                update_stub_status(case_id, status="error_image_too_large", updated_at=datetime.now().isoformat(timespec="seconds"))

                # 移回 pending（讓內網可改成走別條通道或人工處理）
                try:
                    os.rename(dest_path, pending_path(case_id))
                except Exception as e:
                    logger.error(f"Failed to move case back to pending: {e}")

                return {"status": "error", "message": "Image too large to claim"}

            with open(img_path, "rb") as imgf:
                img_bytes = imgf.read()

            # 更新外部狀態為處理中
            update_stub_status(case_id, status="processing", processing_at=datetime.now().isoformat(timespec="seconds"))

            logger.info(f"Case claimed: {case_id}")
            return {
                "status": "ok",
                "data": rec,  # 含個資（僅內網使用）
                "image_b64": base64.b64encode(img_bytes).decode("utf-8"),
                "image_ext": os.path.splitext(img_path)[1].lower() or ".jpg",
            }

        except Exception as e:
            logger.error(f"Error reading claimed case {case_id}: {e}")
            raise HTTPException(status_code=500, detail="Internal processing error")

    return {"status": "empty"}


@app.post("/confirm_case")
def confirm_case(
    case_id: str = Form(...),
    receipt: str = Form(...),
    x_api_key: str = Depends(verify_internal_key),
):
    """
    Step 2: 內網確認已保存資料後，呼叫此 API 刪除外網敏感資料
    - 刪除：processing/{id}.json + uploads/{image}
    - 保留：stub（不含個資）供外網結果頁顯示
    """
    _validate_case_id(case_id)

    json_path = processing_path(case_id)

    # 冪等性：若已刪，回 ok
    if not os.path.exists(json_path):
        return {"status": "ok", "message": "Already deleted or not found"}

    rec = load_json(json_path)

    if rec.get("receipt") != receipt:
        logger.warning(f"Receipt mismatch for confirm_case {case_id}")
        raise HTTPException(status_code=400, detail="Receipt mismatch")

    # 刪除圖片
    img_path = os.path.join(DIRS["uploads"], rec.get("image_filename", ""))
    safe_remove(img_path)

    # 刪除含個資 JSON（processing）
    safe_remove(json_path)

    # 更新 stub 狀態：已清洗
    update_stub_status(case_id, status="data_purged", purged_at=datetime.now().isoformat(timespec="seconds"))

    logger.info(f"Case confirmed and purged: {case_id}")
    return {"status": "ok", "message": "Data purged"}


@app.post("/update_ai_result")
def update_ai_result(
    id: str = Form(...),
    receipt: str = Form(...),
    ai_level: int = Form(...),
    ai_prob: float = Form(...),
    ai_suggestion: str = Form(...),
    x_api_key: str = Depends(verify_internal_key),
):
    """
    Step 3: 內網回寫 AI 結果到 stub
    - 對帳：id + receipt 必須一致
    - 建議：只有在 processing/data_purged 時才允許寫入 done（可避免狀態亂跳）
    """
    _validate_case_id(id)

    sp = stub_path(id)
    if not os.path.exists(sp):
        raise HTTPException(status_code=404, detail="Stub not found")

    stub = load_json(sp)

    if stub.get("receipt") != receipt:
        raise HTTPException(status_code=401, detail="Receipt mismatch")

    # 狀態流轉保護（可選但推薦）
    current_status = stub.get("status")
    if current_status not in ("processing", "data_purged", "pending"):
        logger.warning(f"Unexpected status transition for {id}: {current_status} -> done")

    stub.update(
        {
            "ai_level": ai_level,
            "ai_prob": ai_prob,
            "ai_suggestion": ai_suggestion,
            "status": "done",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )

    save_json(sp, stub)
    logger.info(f"AI Result updated: {id}")

    return {"status": "ok"}
