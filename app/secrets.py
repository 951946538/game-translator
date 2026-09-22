"""密钥安全存储：Windows 凭据管理器（keyring 库封装），API 密钥不落任何明文文件。

分发场景：每个用户首次启动在设置窗口输入自己的密钥，存入本机凭据管理器。"""
import logging

try:
    import keyring
    _AVAILABLE = True
except Exception:  # 极端环境无 keyring：退化为不保存（每次启动重新输入）
    keyring = None
    _AVAILABLE = False

SERVICE = "game-translator"  # 凭据管理器中的服务名


def get_api_key(section="llm"):
    """从凭据管理器读取密钥；不可用时返回空串"""
    if not _AVAILABLE:
        return ""
    try:
        return keyring.get_password(SERVICE, section) or ""
    except Exception:
        return ""


def set_api_key(key, section="llm"):
    """保存密钥到凭据管理器"""
    if not _AVAILABLE or not key:
        return False
    try:
        keyring.set_password(SERVICE, section, key)
        return True
    except Exception:
        logging.error("密钥写入凭据管理器失败", exc_info=True)
        return False


def delete_api_key(section="llm"):
    if not _AVAILABLE:
        return
    try:
        keyring.delete_password(SERVICE, section)
    except Exception:
        pass
