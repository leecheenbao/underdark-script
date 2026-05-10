import uiautomator2 as u2
import os

# 获取脚本所在目录的上级目录（ug项目根目录）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
PIC_DIR = os.path.join(PROJECT_ROOT, "pic")

# 确保pic文件夹存在
os.makedirs(PIC_DIR, exist_ok=True)


from settings import get_default_device

id = get_default_device()

d = u2.connect(id)
screenshot = d.screenshot()
screenshot.save(os.path.join(PIC_DIR, "Pic.png"))