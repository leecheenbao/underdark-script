# UG 自动战斗脚本 - Windows 使用说明

## 目录

1. [你会得到什么文件](#1-你会得到什么文件)
2. [安装 Python](#2-安装-python)
3. [安装 MuMu 模拟器](#3-安装-mumu-模拟器)
4. [安装 ADB 工具](#4-安装-adb-工具)
5. [放置文件](#5-放置文件)
6. [安装 Python 依赖](#6-安装-python-依赖)
7. [配置模拟器连接](#7-配置模拟器连接)
8. [修改脚本配置](#8-修改脚本配置)
9. [运行脚本](#9-运行脚本)
10. [常见问题](#10-常见问题)

---

## 1. 你会得到什么文件

我会给你以下文件，请确认收到：

```
ug/
├── script/
│   ├── auto_fight.py      ← 主脚本（自动战斗）
│   ├── utils.py            ← 工具类（截图、点击等）
│   ├── settings.py         ← 配置加载器
│   └── settings.json       ← 配置文件（需要你自己改）
└── pic/
    ├── enter.png            ← 进入按钮截图
    ├── enter_fight.png      ← 进入战斗按钮截图
    ├── adx2.png             ← ADx2 标识截图
    ├── confirm_fight.png    ← 确认战斗按钮截图
    ├── confirm_rewards.png  ← 确认奖励按钮截图
    ├── captcha.png          ← 验证码弹窗截图
    └── confirm.png          ← 确认按钮截图
```

> **注意**: `pic/` 文件夹里的图片是从游戏里截取的模板图，脚本靠这些图来识别屏幕上的按钮。

---

## 2. 安装 Python

脚本需要 Python 3.8 或更高版本。

### 步骤

1. 打开浏览器，访问 Python 官网下载页面：
   ```
   https://www.python.org/downloads/
   ```

2. 点击黄色的 **Download Python 3.x.x** 按钮，下载安装包

3. 双击运行安装包，在安装界面：
   - **最重要的一步** ：勾选底部的 **"Add Python to PATH"** 复选框
   - 然后点击 **Install Now**

   ![Add to PATH](https://docs.python.org/3/_images/win_installer.png)

4. 等待安装完成，点击 **Close**

### 验证安装

1. 按键盘 `Win + R`，输入 `cmd`，按回车，打开命令提示符
2. 输入以下命令并回车：
   ```
   python --version
   ```
3. 如果看到类似 `Python 3.12.x` 的输出，说明安装成功
4. 如果提示"不是内部或外部命令"，说明安装时没勾选 **Add to PATH**，需要重新安装

---

## 3. 安装 MuMu 模拟器

### 步骤

1. 打开浏览器，访问 MuMu 模拟器官网：
   ```
   https://mumu.163.com/
   ```

2. 下载并安装 **MuMu 模拟器 12**（MuMu Player 12）

3. 安装完成后打开 MuMu 模拟器

4. 在模拟器里安装 UG 游戏（UnderGuild/地下攻防战）

### 开启 ADB 调试

MuMu 12 默认已开启 ADB，但请确认一下：

1. 点击 MuMu 模拟器右上角的 **菜单（三条横线）**
2. 进入 **设置**
3. 找到 **其他设置** 或 **开发者选项**
4. 确认 **ADB 调试** 是开启状态

### 查看 ADB 端口

1. 在 MuMu 模拟器主界面，点击右上角菜单
2. 找到 **问题诊断** 或在设置中查看
3. 记下显示的 **ADB 端口号**（通常是 `16384`）
4. 那么你的设备 ID 就是 `127.0.0.1:16384`

> 如果你有多个模拟器实例，第二个通常是 `16416`，第三个是 `16448`，以此类推。

---

## 4. 安装 ADB 工具

ADB（Android Debug Bridge）是用来连接和控制模拟器的工具。

### 方法一：使用 MuMu 自带的 ADB（推荐）

MuMu 12 自带 ADB 工具，路径通常是：
```
C:\Program Files\Netease\MuMu Player 12\shell\adb.exe
```

你需要把这个路径加入系统 PATH：

1. 按 `Win + R`，输入 `sysdm.cpl`，回车
2. 点击 **高级** 选项卡
3. 点击 **环境变量**
4. 在 **系统变量** 中找到 `Path`，双击它
5. 点击 **新建**，输入 ADB 所在的文件夹路径：
   ```
   C:\Program Files\Netease\MuMu Player 12\shell
   ```
6. 一路点 **确定** 保存

### 方法二：单独下载 ADB

1. 打开浏览器，访问：
   ```
   https://developer.android.com/tools/releases/platform-tools
   ```
2. 下载 **SDK Platform-Tools for Windows**
3. 解压到一个方便的位置，例如 `C:\adb`
4. 按照上面方法一的步骤，把 `C:\adb` 加入系统 PATH

### 验证 ADB

1. **关闭之前打开的所有命令提示符窗口**（改了 PATH 之后必须重开）
2. 按 `Win + R`，输入 `cmd`，回车，打开新的命令提示符
3. 输入：
   ```
   adb version
   ```
4. 如果看到版本号输出（如 `Android Debug Bridge version 1.0.41`），说明安装成功

---

## 5. 放置文件

### 步骤

1. 在你的电脑上创建一个文件夹，例如在 D 盘创建：
   ```
   D:\ug
   ```

2. 把收到的文件按以下结构放好：
   ```
   D:\ug\
   ├── script\
   │   ├── auto_fight.py
   │   ├── utils.py
   │   ├── settings.py
   │   └── settings.json
   └── pic\
       ├── enter.png
       ├── enter_fight.png
       ├── adx2.png
       ├── confirm_fight.png
       ├── confirm_rewards.png
       ├── captcha.png
       └── confirm.png
   ```

> **重要**: `script` 和 `pic` 两个文件夹必须在同一个父目录下（都在 `ug` 里面）。脚本通过相对路径 `../pic/` 查找图片。

---

## 6. 安装 Python 依赖

### 打开命令提示符

1. 按 `Win + R`，输入 `cmd`，按回车

### 安装依赖包

在命令提示符中依次输入以下命令（每输一行按一次回车，等它装完再输下一行）：

```
pip install opencv-python
```
```
pip install numpy
```
```
pip install ddddocr
```
```
pip install requests
```

> `opencv-python` 是图像识别库，`ddddocr` 是验证码识别库，`requests` 用于微信通知（可选功能）。

### 如果安装很慢

可以使用国内镜像源加速，把命令改成：

```
pip install opencv-python -i https://pypi.tuna.tsinghua.edu.cn/simple
```
```
pip install numpy -i https://pypi.tuna.tsinghua.edu.cn/simple
```
```
pip install ddddocr -i https://pypi.tuna.tsinghua.edu.cn/simple
```
```
pip install requests -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 验证安装

```
python -c "import cv2; import ddddocr; print('依赖安装成功')"
```

如果输出"依赖安装成功"就没问题。

---

## 7. 配置模拟器连接

### 连接模拟器

1. 确保 MuMu 模拟器已经打开并完全启动
2. 打开命令提示符，输入：
   ```
   adb connect 127.0.0.1:16384
   ```
   （如果你在第3步查到的端口不是 16384，替换成你的端口号）

3. 输入以下命令确认连接成功：
   ```
   adb devices
   ```

4. 你应该看到类似这样的输出：
   ```
   List of devices attached
   127.0.0.1:16384    device
   ```
   如果状态显示 `device`，说明连接成功。

---

## 8. 修改脚本配置

### 修改 settings.json

用记事本打开 `D:\ug\script\settings.json`（右键 → 打开方式 → 记事本）：

```json
{
  "emulators": {
    "1": {"id": "127.0.0.1:16384", "name": "模拟器1"}
  },
  "default_emulator": "1",
  "proxy": {
    "host": "10.0.2.2",
    "port": 8080
  },
  "game": {
    "package": "com.FreeDust.UnderDarkOffense"
  },
  "cert": {
    "hash": "c8750f0d",
    "pem_path": "~/.mitmproxy/mitmproxy-ca-cert.pem"
  }
}
```

**你只需要改一个地方**：把 `127.0.0.1:16384` 改成你自己模拟器的 ADB 地址（第3步查到的端口）。

> 如果你的端口也是 16384，那就不用改，直接用就行。

### 修改战斗次数（可选）

用记事本打开 `D:\ug\script\auto_fight.py`，找到第 31 行：

```python
NUM = 50
```

把 `50` 改成你想要的战斗次数。例如想打 100 次就改成：

```python
NUM = 100
```

### 关于微信通知（可选）

`utils.py` 中有一个微信通知功能，使用 PushPlus 平台推送。如果你不需要微信通知，打开 `utils.py`，找到大约第 26 行：

```python
ENABLE_WECHAT = True
```

改成：

```python
ENABLE_WECHAT = False
```

如果你想用微信通知，需要：
1. 访问 http://www.pushplus.plus/ 注册并获取你自己的 Token
2. 把 `utils.py` 第 25 行的 Token 替换成你自己的

---

## 9. 运行脚本

### 准备工作

1. 确保 MuMu 模拟器已打开
2. 确保游戏已启动，并且停在可以点"进入"按钮的界面
3. 确保 ADB 已连接（参考第7步）

### 运行

1. 打开命令提示符（`Win + R` → 输入 `cmd` → 回车）

2. 进入脚本目录：
   ```
   cd /d D:\ug\script
   ```
   （如果你放在其他位置，替换成你的实际路径）

3. 运行脚本：
   ```
   python auto_fight.py
   ```

4. 脚本开始运行后，你会看到类似这样的日志输出：
   ```
   [14:30:01] ==================================================
   [14:30:01] UG 自动战斗脚本
   [14:30:01] ==================================================
   [14:30:01] 設備已連接127.0.0.1:16384
   [14:30:01] ==================================================
   [14:30:01] 第 1 次战斗开始
   [14:30:01] ==================================================
   [14:30:01] [步骤1] 点击进入
   ...
   ```

5. **运行过程中不要操作模拟器**，让脚本自己跑

### 停止脚本

按 `Ctrl + C` 即可停止脚本。

---

## 10. 常见问题

### Q: 提示 "python 不是内部或外部命令"
**A**: 安装 Python 时没有勾选 "Add to PATH"。重新运行 Python 安装包，选择 **Modify**，确保勾选了 PATH 选项。或者卸载重装，这次记得勾选。

### Q: 提示 "adb 不是内部或外部命令"
**A**: ADB 没有加入系统 PATH。回到第4步，按照步骤把 ADB 路径加入环境变量。**加完之后必须关掉命令提示符重新打开**才会生效。

### Q: `adb devices` 显示为空
**A**:
- 确认 MuMu 模拟器已经完全启动（不是在加载中）
- 尝试重新连接：`adb connect 127.0.0.1:16384`
- 如果端口不对，回到第3步确认你的 ADB 端口号

### Q: `adb devices` 显示 `unauthorized`
**A**: 模拟器上弹出了 ADB 授权确认弹窗，在模拟器里点击"允许"。

### Q: 脚本报错 "设备连接失败"
**A**:
- 运行 `adb devices` 确认设备状态是 `device`
- 确认 `settings.json` 里的设备 ID 和你的模拟器端口一致

### Q: 脚本运行但是不点击 / 点错位置
**A**: 模板图片可能和你的游戏画面不匹配。可能的原因：
- 模拟器分辨率不对。脚本基于 **1080x1920** 分辨率制作，请在 MuMu 模拟器设置中把分辨率改为 1080x1920
- 游戏界面有更新。需要重新截图制作模板

### Q: 验证码一直识别失败
**A**:
- 确认已安装 ddddocr：`pip install ddddocr`
- 验证码识别不是 100% 准确，脚本会自动重试 3 次
- 如果经常失败，可能需要更新 ddddocr：`pip install --upgrade ddddocr`

### Q: pip install 报错 "没有找到满足要求的版本"
**A**: Python 版本太低。ddddocr 需要 Python 3.8+。运行 `python --version` 检查版本。

### Q: 脚本卡在 "等待xxx..." 不动了
**A**: 脚本在等待某个画面出现。可能是游戏界面没有进入正确的状态。按 `Ctrl + C` 停止脚本，手动把游戏调整到正确的界面，然后重新运行。

### Q: 分辨率太低的話重新截圖
**A**: 使用指令透過 adb 取得圖片
- `adb -s 127.0.0.1:16384 shell screencap -p /sdcard/screen.png` 
- `adb -s 127.0.0.1:16384 pull /sdcard/screen.png .`
---

## 附录：脚本战斗流程说明

脚本每一轮战斗会按以下顺序执行：

```
1. 点击"进入"按钮（enter.png）
2. 等待"进入战斗"按钮出现并点击（enter_fight.png）
3. 检查是否弹出验证码 → 如果有，自动识别并输入
4. 战斗中，等待 ADx2 标识出现并点击（adx2.png）
5. 等待 ADx2 消失（战斗结束）
6. 点击"确认战斗"（confirm_fight.png）
7. 点击"确认奖励"（confirm_rewards.png）
8. 回到第1步，开始下一轮
```

完成所有轮次后脚本自动结束。
