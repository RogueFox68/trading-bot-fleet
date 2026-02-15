import logging
import sys
from logging.handlers import RotatingFileHandler
import os

# --- CONFIGURATION ---
LOG_FILE = "fleet.log"
MAX_BYTES = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 3

def get_logger(name):
    """
    Returns a configured logger instance.
    """
    logger = logging.getLogger(name)
    
    # If logger already has handlers, assume it's configured and return it
    if logger.hasHandlers():
        return logger
        
    logger.setLevel(logging.INFO)

    # 1. File Handler (Rotation)
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding='utf-8'
    )
    file_formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # 2. Console Handler (Safe)
    console_handler = SafeStreamHandler(sys.stdout)
    console_formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    return logger

class SafeStreamHandler(logging.StreamHandler):
    """
    A StreamHandler that handles UnicodeEncodeError by replacing unprintable characters.
    """
    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            # Try writing normally
            stream.write(msg + self.terminator)
            self.flush()
        except UnicodeEncodeError:
            try:
                # If it fails, encode with replacement
                encoding = getattr(stream, 'encoding', 'ascii') or 'ascii'
                msg_safe = msg.encode(encoding, errors='replace').decode(encoding)
                stream.write(msg_safe + self.terminator)
                self.flush()
            except Exception:
                self.handleError(record)
        except Exception:
            self.handleError(record)

# Create a default logger for quick imports
logger = get_logger("FLEET")
