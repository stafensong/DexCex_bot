import sys
import orjson
from loguru import logger

from ..core.config import ObservabilityConfig

def setup_logging(config: ObservabilityConfig):
    """
    設定 loguru 進行結構化日誌記錄。
    """
    logger.remove() # 移除預設的 handler

    log_level = config.log_level.upper()
    redact_keys = config.redact_keys

    def serialize(record):
        """自定義序列化函數，用於將日誌記錄轉為 JSON，並進行脫敏。"""
        subset = {
            "timestamp": record["time"].isoformat(),
            "level": record["level"].name,
            "message": record["message"],
            "name": record["name"],
            "function": record["function"],
            "line": record["line"],
        }
        # 添加額外數據並進行脫敏
        if record["extra"]:
            for key, value in record["extra"].items():
                if any(redact_key in key.lower() for redact_key in redact_keys):
                    subset[key] = "[REDACTED]"
                else:
                    subset[key] = value
        
        # 處理異常
        if record["exception"]:
            subset["exception"] = str(record["exception"])

        return orjson.dumps(subset).decode('utf-8')

    def sink(message):
        """將序列化後的日誌輸出到 stdout。"""
        serialized = serialize(logger.patch(lambda record: record.update(message=message)).__dict__)
        print(serialized, file=sys.stdout)

    # logger.add(sink, level=log_level, format="{message}")
    # 為了在開發時有更好的可讀性，暫時使用標準格式
    logger.add(sys.stdout, level=log_level)

    # logger.info(f"日誌系統已設定，級別: {log_level}")
    return logger
