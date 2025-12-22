import os
import json
import uuid
import glob
import logging
import base64
import shutil
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

# 外網限制（避免被大檔打爆）
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", "8000000"))  # 8MB
MAX_CLAIM_IMAGE_BYTES = int(os.getenv("MAX_CLAIM_IMAGE_BYTES", "8000000"))  # 8MB

# 資料夾結構定義
# pending: 用戶剛上傳，等待內網抓取
# processing: 內網正在抓取中（鎖定狀態），等待 Confirm 刪除
# stubs: 僅存狀態與結果，不含個資，永久保留
# error: 缺圖/過大等不可處理案件隔離，避免卡住 processing 佇列
DIRS = {
    "uploads": "storage/uploads",       # 原始圖片 (Confirm 後刪除)
    "pending": "storage/pending",       # 完整案件 JSON (等待中)
    "processing": "storage/processing", # 處理中案件 JSON (鎖定中)
    "stubs": "storage/stubs",           # 存根 JSON (永久保留，公開結果)
    "error": "storage/error",           # 錯誤隔離區（缺圖/過大等不可處理案件）
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
    """
    if x_api_key != INTERNAL_API_KEY:
        logger.warning(f"Invalid API Key attempt: {x_api_key}")
        raise HTTPException(status_code=401, detail="Unauthorized")
    return x_api_key

# ============================
# 3) 工具函式
# ============================
def guess_ext(filename: str, content_type: str) -> str:
    """推測副檔名，預設為 .jpg"""
    filename = (filename or "").lower()
    content_type = (content_type or "").lower()

    if filename.endswith(".png") or content_type == "image/png":
        return ".png"
    if filename.endswith(".jpeg") or content_type == "image/jpeg":
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

def stub_path(case_id: str) -> str:
    return os.path.join(DIRS["stubs"], f"{case_id}.json")

def pending_path(case_id: str) -> str:
    return os.path.join(DIRS["pending"], f"{case_id}.json")

def processing_path(case_id: str) -> str:
    return os.path.join(DIRS["processing"], f"{case_id}.json")

def error_path(case_id: str) -> str:
    return os.path.join(DIRS["error"], f"{case_id}.json")

def update_stub_status(case_id: str, **fields):
    sp = stub_path(case_id)
    if not os.path.exists(sp):
        return
    try:
        stub = load_json(sp)
        stub.update(fields)
        save_json(sp, stub)
    except Exception as e:
        logger.error(f"Failed to update stub {case_id}: {e}")

# ============================
# 4) Routes（外網：給家屬使用）
# ============================

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    # 簡單首頁，引導去填單
    return """
    <html>
        <head><title>Diaper Rash AI</title></head>
        <body style="font-family: sans-serif; text-align: center; padding: 50px;">
            <h1>Diaper Rash AI Service</h1>
            <p>System Status: <span style='color:green'>Online</span></p>
            <a href='/form' style="background: #007bff; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Submit Case</a>
        </body>
    </html>
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
    - 存完整 JSON 到 pending（含個資）
    - 存照片到 uploads
    - 存 stub 到 stubs（不含個資）
    """
    case_id = str(uuid.uuid4())
    receipt = uuid.uuid4().hex  # 對帳 Token
    created_at = datetime.now().isoformat(timespec="seconds")

    # 檔案大小限制（若 client 有帶 content-length）
    try:
        if image.size and int(image.size) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File too large")
    except Exception:
        # 有些環境 UploadFile 沒 size，略過
        pass

    # 儲存圖片（chunked write）
    ext = guess_ext(image.filename, image.content_type)
    img_filename = f"{case_id}{ext}"
    img_path = os.path.join(DIRS["uploads"], img_filename)

    try:
        with open(img_path, "wb") as f:
            while True:
                chunk = image.file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:
        logger.error(f"Image save failed: {e}")
        raise HTTPException(status_code=500, detail="File upload failed")
    finally:
        try:
            image.file.close()
        except Exception:
            pass

    # 存完整案件（含個資）到 pending
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

    # 存 stub（不含個資）
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
    [外網] 結果頁：只讀 stub（不含個資、不顯示照片也可）
    """
    sp = stub_path(case_id)
    if not os.path.exists(sp):
        return templates.TemplateResponse(
            "error.html", {"request": request, "message": "Case not found"}
        )
    stub = load_json(sp)
    return templates.TemplateResponse("result.html", {"request": request, "case": stub})

# ==========================================
# 5) 內網專用 API（Two-Phase Commit）
# ==========================================

@app.post("/claim_case")
def claim_case(x_api_key: str = Depends(verify_internal_key)):
    """
    Step 1: 內網請求案件
    - pending -> processing (原子 rename 鎖定)
    - 回傳完整資料 + 圖片 base64
    - ⚠️ 若缺圖/過大：把 processing JSON 移到 error 區隔離，避免卡住佇列
    """
    pending_files = glob.glob(os.path.join(DIRS["pending"], "*.json"))
    pending_files.sort(key=lambda p: os.path.getmtime(p))

    for _ in range(5):
        if not pending_files:
            return {"status": "empty"}

        target_file = pending_files[0]
        filename = os.path.basename(target_file)
        case_id = os.path.splitext(filename)[0]
        dest_path = processing_path(case_id)

        try:
            os.rename(target_file, dest_path)
        except OSError:
            # 被別人搶走
            pending_files = pending_files[1:]
            continue

        # --- 鎖定成功 ---
        try:
            rec = load_json(dest_path)
            img_path = os.path.join(DIRS["uploads"], rec.get("image_filename", ""))
            receipt = rec.get("receipt", "")

            # 檢查圖片是否存在：缺圖 -> 移到 error 區，避免卡在 processing
            if not os.path.exists(img_path):
                logger.error(f"Image missing: {case_id}")
                update_stub_status(
                    case_id,
                    status="error",
                    note="Image missing (quarantined)",
                    error_at=datetime.now().isoformat(timespec="seconds"),
                )
                try:
                    os.rename(dest_path, error_path(case_id))
                except Exception as e:
                    logger.error(f"Failed to quarantine (missing image) {case_id}: {e}")

                return {
                    "status": "error",
                    "message": "Image file missing",
                    "case_id": case_id,
                    "receipt": receipt,
                }

            # 檢查大小：過大也隔離，避免無限重試
            if os.path.getsize(img_path) > MAX_CLAIM_IMAGE_BYTES:
                logger.error(f"Image too large: {case_id}")
                update_stub_status(
                    case_id,
                    status="error",
                    note="Image too large (quarantined)",
                    error_at=datetime.now().isoformat(timespec="seconds"),
                )
                try:
                    os.rename(dest_path, error_path(case_id))
                except Exception as e:
                    logger.error(f"Failed to quarantine (too large) {case_id}: {e}")

                return {
                    "status": "error",
                    "message": "Image too large",
                    "case_id": case_id,
                    "receipt": receipt,
                }

            with open(img_path, "rb") as imgf:
                img_bytes = imgf.read()

            update_stub_status(case_id, status="processing", processing_at=datetime.now().isoformat(timespec="seconds"))

            logger.info(f"Case claimed: {case_id}")
            return {
                "status": "ok",
                "data": rec,
                "image_b64": base64.b64encode(img_bytes).decode("utf-8"),
                "image_ext": os.path.splitext(img_path)[1].lower() or ".jpg",
            }

        except Exception as e:
            logger.error(f"Error reading claimed case {case_id}: {e}")
            # 讀取錯誤：為了避免卡住，也隔離 processing JSON
            try:
                os.rename(dest_path, error_path(case_id))
            except Exception as e2:
                logger.error(f"Failed to quarantine (read error) {case_id}: {e2}")
            update_stub_status(case_id, status="error", note="Read error (quarantined)")
            return {"status": "error", "message": "Internal claim read error", "case_id": case_id}

    return {"status": "empty"}

@app.post("/confirm_case")
def confirm_case(
    case_id: str = Form(...),
    receipt: str = Form(...),
    x_api_key: str = Depends(verify_internal_key),
):
    """
    Step 2: 內網確認已保存資料，通知外網刪除個資
    - 刪 processing JSON（含個資）
    - 刪 uploads 圖片
    """
    json_path = processing_path(case_id)

    if not os.path.exists(json_path):
        return {"status": "ok", "message": "Already deleted or not found"}

    rec = load_json(json_path)

    if rec.get("receipt") != receipt:
        raise HTTPException(status_code=400, detail="Receipt mismatch")

    # 刪圖片
    img_path = os.path.join(DIRS["uploads"], rec.get("image_filename", ""))
    if os.path.exists(img_path):
        try:
            os.remove(img_path)
        except Exception as e:
            logger.warning(f"Failed to remove image {img_path}: {e}")

    # 刪 processing JSON（個資）
    try:
        os.remove(json_path)
    except Exception as e:
        logger.warning(f"Failed to remove json {json_path}: {e}")

    update_stub_status(case_id, status="data_purged", updated_at=datetime.now().isoformat(timespec="seconds"))

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
    Step 3: 內網回寫 AI 結果到 stub（stub 不含個資）
    """
    sp = stub_path(id)
    if not os.path.exists(sp):
        raise HTTPException(status_code=404, detail="Stub not found")

    stub = load_json(sp)

    if stub.get("receipt") != receipt:
        raise HTTPException(status_code=401, detail="Receipt mismatch")

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

@app.post("/abort_case")
def abort_case(
    case_id: str = Form(...),
    receipt: str = Form(...),
    x_api_key: str = Depends(verify_internal_key),
):
    """
    內網在存圖/寫 DB 失敗時呼叫：
    - processing -> pending（讓下次可重試）
    """
    proc_path = processing_path(case_id)
    if not os.path.exists(proc_path):
        raise HTTPException(status_code=404, detail="Case not in processing")

    rec = load_json(proc_path)
    if rec.get("receipt") != receipt:
        raise HTTPException(status_code=400, detail="Receipt mismatch")

    try:
        os.rename(proc_path, pending_path(case_id))
        update_stub_status(
            case_id,
            status="pending",
            note="Aborted by internal worker (retry scheduled)",
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
        logger.warning(f"Case aborted and moved back to pending: {case_id}")
        return {"status": "ok", "message": "Case moved back to pending"}
    except Exception as e:
        logger.error(f"Failed to abort case {case_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to move file")
