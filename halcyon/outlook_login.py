"""One-time Outlook sign-in for the order agent:  python -m halcyon.outlook_login"""
from .live import load_env
from .outlook import device_login

if __name__ == "__main__":
    load_env()
    device_login()
