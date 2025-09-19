import orjson
from typing import Dict, Any
from loguru import logger

STATE_FILE_PATH = "data/state.json"

def save_state(state: Dict[str, Any]):
    """將機器人的狀態保存到 JSON 檔案中。"""
    try:
        with open(STATE_FILE_PATH, 'wb') as f:
            f.write(orjson.dumps(state, option=orjson.OPT_INDENT_2))
        logger.debug(f"狀態已成功保存到 {STATE_FILE_PATH}")
    except Exception as e:
        logger.error(f"保存狀態失敗: {e}")

def load_state() -> Dict[str, Any]:
    """從 JSON 檔案中載入機器人的狀態。"""
    try:
        with open(STATE_FILE_PATH, 'rb') as f:
            state = orjson.loads(f.read())
            logger.info(f"已從 {STATE_FILE_PATH} 載入先前狀態。")
            return state
    except FileNotFoundError:
        logger.warning(f"狀態檔案 {STATE_FILE_PATH} 未找到，將使用初始狀態。")
        return {}
    except Exception as e:
        logger.error(f"載入狀態失敗: {e}")
        return {}
