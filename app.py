import cv2
import numpy as np
import os
import requests
import base64
import time
import json
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageDraw, ImageFont
from flask import Flask, Response, render_template, jsonify, request
import threading
from dotenv import load_dotenv  # 添加dotenv支持

# 加载环境变量
load_dotenv()

# ====================== Flask 应用初始化 ======================
app = Flask(__name__)

# ====================== 全局变量 ======================
latest_frame = None
latest_result = {"object": "等待识别", "type": "", "confidence": 0.0}
frame_lock = threading.Lock()
result_lock = threading.Lock()
camera_active = True

# ====================== API 配置 ======================
# 从环境变量获取配置
BAIDU_API_KEY = os.getenv("BAIDU_API_KEY")
BAIDU_SECRET_KEY = os.getenv("BAIDU_SECRET_KEY")
BAIDU_API_URL = "https://aip.baidubce.com/rest/2.0/image-classify/v2/advanced_general"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

# ====================== 环境配置 ======================
os.environ['QT_QPA_PLATFORM'] = 'xcb'
os.environ['OPENCV_GUI_BACKEND'] = 'SDL'

# 从环境变量获取字体配置
FONT_PATH = os.getenv("FONT_PATH", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc")
FONT_SIZE = int(os.getenv("FONT_SIZE", "24"))
font = ImageFont.truetype(FONT_PATH, FONT_SIZE)

# ====================== 摄像头参数 ======================
# 从环境变量获取摄像头配置
CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
TARGET_WIDTH = int(os.getenv("TARGET_WIDTH", "640"))
TARGET_HEIGHT = int(os.getenv("TARGET_HEIGHT", "480"))
CAMERA_WIDTH = int(os.getenv("CAMERA_WIDTH", "320"))
CAMERA_HEIGHT = int(os.getenv("CAMERA_HEIGHT", "240"))
FPS = int(os.getenv("FPS", "10"))
API_INTERVAL = float(os.getenv("API_INTERVAL", "2.0"))

# ====================== 垃圾类别映射 ======================
GARBAGE_MAPPING = {
    "塑料瓶": {"type": "可回收物", "confidence": 0.6},
    "易拉罐": {"type": "可回收物", "confidence": 0.6},
    "玻璃瓶": {"type": "可回收物", "confidence": 0.6},
    "纸张": {"type": "可回收物", "confidence": 0.5},
    "快递盒": {"type": "可回收物", "confidence": 0.5},
    "电池": {"type": "有害垃圾", "confidence": 0.7},
    "灯泡": {"type": "有害垃圾", "confidence": 0.7},
    "药品": {"type": "有害垃圾", "confidence": 0.6},
    "杀虫剂": {"type": "有害垃圾", "confidence": 0.6},
    "果皮": {"type": "厨余垃圾", "confidence": 0.5},
    "蔬菜": {"type": "厨余垃圾", "confidence": 0.5},
    "剩饭": {"type": "厨余垃圾", "confidence": 0.5},
    "骨头": {"type": "厨余垃圾", "confidence": 0.5},
    "塑料袋": {"type": "其他垃圾", "confidence": 0.5},
    "纸巾": {"type": "其他垃圾", "confidence": 0.5},
    "砖瓦": {"type": "其他垃圾", "confidence": 0.5},
    "烟头": {"type": "其他垃圾", "confidence": 0.5},
}

# ====================== 百度 API 工具函数 ======================
def get_baidu_access_token():
    cache_file = "baidu_access_token.txt"
    try:
        with open(cache_file, "r") as f:
            token, expire_time = f.read().split()
            if float(expire_time) > time.time():
                print("使用百度缓存 Token")
                return token
    except:
        print("百度缓存 Token 失效，重新获取...")
    
    for _ in range(3):
        try:
            response = requests.get(
                "https://aip.baidubce.com/oauth/2.0/token",
                params={
                    "grant_type": "client_credentials",
                    "client_id": BAIDU_API_KEY,
                    "client_secret": BAIDU_SECRET_KEY
                },
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            access_token = data["access_token"]
            expire_time = time.time() + data["expires_in"] - 60
            with open(cache_file, "w") as f:
                f.write(f"{access_token} {expire_time}")
            print("百度新 Token 获取成功")
            return access_token
        except Exception as e:
            print(f"百度 Token 获取失败: {e}")
            print(f"响应内容: {response.text if response else '无响应'}")
            time.sleep(2)
    raise Exception("百度 Token 获取失败，请检查网络或密钥！")

def baidu_classify_object(image_base64):
    access_token = get_baidu_access_token()
    request_url = f"{BAIDU_API_URL}?access_token={access_token}"
    
    payload = {
        "image": image_base64,
        "image_type": "BASE64",
        "baike_num": 0,
        "max_results": 3
    }
    
    for attempt in range(3):
        try:
            print(f"百度 API 调用尝试 {attempt+1}/3...")
            response = requests.post(request_url, data=payload, timeout=5)
            response.raise_for_status()
            result = response.json()
            if "error_code" in result:
                print(f"百度 API 错误: {result['error_code']} - {result['error_msg']}")
                continue
            if "result" in result and len(result["result"]) > 0:
                best_result = max(result["result"], key=lambda x: x.get("score", 0))
                return {
                    "keyword": best_result.get("keyword", "未识别"),
                    "score": best_result.get("score", 0.0)
                }
            else:
                print("百度 API 返回空结果")
                return None
        except requests.Timeout:
            print("百度 API 请求超时，重试...")
        except Exception as e:
            print(f"百度 API 调用失败: {e}")
            print(f"响应内容: {response.text if response else '无响应'}")
        time.sleep(1)
    return None

def process_recognition_result(keyword, score):
    mapping = GARBAGE_MAPPING.get(keyword)
    if mapping and score >= mapping["confidence"]:
        return {
            "object": keyword,
            "type": mapping["type"],
            "confidence": score
        }
    else:
        return {
            "object": keyword,
            "type": "其他垃圾" if score > 0.5 else "未识别",
            "confidence": score
        }

# ====================== DeepSeek API 工具函数 ======================
def ask_deepseek(question):
    """调用 DeepSeek API 获取垃圾分类知识"""
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # 优化提示词，让DeepSeek专注于垃圾分类知识
    prompt = (
        f"你是一个垃圾分类专家，请用简洁的语言回答以下问题：{question}\n"
        "回答要求：\n"
        "1. 只回答与垃圾分类相关的内容\n"
        "2. 保持回答在100字以内\n"
        "3. 使用中文回答\n"
        "4. 包含正确的垃圾分类类别和处理建议"
    )
    
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,  # 降低随机性，确保准确性
        "max_tokens": 300
    }
    
    try:
        print(f"正在查询DeepSeek: {question}")
        response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if "choices" in data and len(data["choices"]) > 0:
            return data["choices"][0]["message"]["content"]
        else:
            return "未能获取垃圾分类信息，请稍后再试"
    except Exception as e:
        print(f"DeepSeek API调用失败: {e}")
        return "垃圾分类知识查询服务暂时不可用"

# ====================== 摄像头处理线程 ======================
def camera_processing():
    global latest_frame, latest_result, camera_active
    
    cap = None
    executor = ThreadPoolExecutor(max_workers=1)
    api_future = None
    last_api_time = 0
    
    try:
        # 增加摄像头初始化重试逻辑
        max_attempts = 5
        for attempt in range(max_attempts):
            cap = cv2.VideoCapture(CAMERA_INDEX)
            if cap.isOpened():
                print(f"摄像头成功打开，尝试 {attempt+1}/{max_attempts}")
                break
            else:
                print(f"摄像头打开失败，尝试 {attempt+1}/{max_attempts}...")
                time.sleep(2)
                if attempt == max_attempts - 1:
                    raise Exception("摄像头无法打开！")
        
        # 设置摄像头参数
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, FPS)
        
        # 检查实际设置是否生效
        actual_width = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        actual_height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        print(f"摄像头参数设置: 宽度={actual_width}, 高度={actual_height}, FPS={actual_fps}")
        
        print("=== 垃圾分类识别已启动 ===")
        print(f"设备号: /dev/video{CAMERA_INDEX}")
        
        while camera_active:
            ret, raw_frame = cap.read()
            if not ret:
                print("警告：未读取到帧...")
                time.sleep(0.1)
                continue
            
            # 缩放帧
            frame = cv2.resize(raw_frame, (TARGET_WIDTH, TARGET_HEIGHT))
            
            # 处理识别结果
            current_time = time.time()
            if current_time - last_api_time >= API_INTERVAL:
                if api_future is None or api_future.done():
                    _, encoded = cv2.imencode('.jpg', raw_frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
                    img_base64 = base64.b64encode(encoded).decode('utf-8')
                    api_future = executor.submit(baidu_classify_object, img_base64)
                    last_api_time = current_time
            
            # 更新识别结果
            if api_future and api_future.done():
                object_info = api_future.result()
                if object_info:
                    with result_lock:
                        latest_result = process_recognition_result(
                            object_info["keyword"], 
                            object_info["score"]
                        )
                else:
                    with result_lock:
                        latest_result = {"object": "识别失败", "type": "未知", "confidence": 0.0}
            
            # 绘制结果到帧上
            frame_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            draw = ImageDraw.Draw(frame_pil)
            
            with result_lock:
                result = latest_result
                
            text_lines = [
                f"物体: {result['object']}",
                f"分类: {result['type']}",
                f"置信度: {result['confidence']:.2f}",
            ]
            
            for i, line in enumerate(text_lines):
                y = 30 + i * 30
                draw.text((10, y), line, font=font, fill=(0, 255, 0))
            
            # 转换为OpenCV格式并更新全局帧
            frame_with_text = cv2.cvtColor(np.array(frame_pil), cv2.COLOR_RGB2BGR)
            
            with frame_lock:
                _, buffer = cv2.imencode('.jpg', frame_with_text)
                latest_frame = buffer.tobytes()
                
            time.sleep(0.05)  # 控制循环频率
            
    except Exception as e:
        print(f"摄像头处理异常: {str(e)}")
        # 添加详细的错误信息
        print("可能的解决方案:")
        print("1. 检查摄像头是否正确连接")
        print("2. 运行 'ls /dev/video*' 确认设备号")
        print("3. 尝试修改 .env 文件中的 CAMERA_INDEX 为其他值 (0, 1, 2)")
        print("4. 确保用户有访问摄像头的权限: sudo usermod -a -G video $USER")
        print("5. 重启树莓派")
    finally:
        if cap is not None:
            cap.release()
        print("摄像头处理线程退出")

# ====================== Flask 路由 ======================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    def generate():
        frame_count = 0
        last_frame_time = time.time()
        
        while camera_active:
            with frame_lock:
                if latest_frame:
                    try:
                        # 发送帧数据
                        yield (b'--frame\r\n'
                               b'Content-Type: image/jpeg\r\n\r\n' + latest_frame + b'\r\n')
                        
                        # 计算帧率
                        frame_count += 1
                        current_time = time.time()
                        if current_time - last_frame_time >= 1.0:
                            fps = frame_count / (current_time - last_frame_time)
                            print(f"视频流帧率: {fps:.2f} FPS")
                            frame_count = 0
                            last_frame_time = current_time
                    except Exception as e:
                        print(f"视频流发送错误: {str(e)}")
                else:
                    # 没有帧时发送占位图像
                    placeholder = np.zeros((TARGET_HEIGHT, TARGET_WIDTH, 3), dtype=np.uint8)
                    cv2.putText(placeholder, "摄像头未就绪", (50, TARGET_HEIGHT//2), 
                                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                    _, buffer = cv2.imencode('.jpg', placeholder)
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            
            time.sleep(0.05)  # 控制帧率
    
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/get_result')
def get_result():
    with result_lock:
        return jsonify(latest_result)

@app.route('/ask_deepseek', methods=['POST'])
def ask_deepseek_route():
    data = request.get_json()
    question = data.get('question', '')
    
    if not question:
        return jsonify({"error": "缺少问题参数"}), 400
    
    # 使用DeepSeek API获取垃圾分类知识
    answer = ask_deepseek(question)
    return jsonify({"answer": answer})

@app.route('/stop')
def stop_camera():
    global camera_active
    camera_active = False
    return "摄像头已停止"

# ====================== 主函数 ======================
if __name__ == '__main__':
    # 打印配置信息
    print("="*50)
    print("垃圾分类识别系统配置:")
    print(f"百度API密钥: {BAIDU_API_KEY[:4]}...{BAIDU_API_KEY[-4:]}")
    print(f"DeepSeek API密钥: {DEEPSEEK_API_KEY[:4]}...{DEEPSEEK_API_KEY[-4:]}")
    print(f"摄像头设备号: {CAMERA_INDEX}")
    print(f"分辨率: {CAMERA_WIDTH}x{CAMERA_HEIGHT} → {TARGET_WIDTH}x{TARGET_HEIGHT}")
    print(f"帧率: {FPS} FPS")
    print(f"API调用间隔: {API_INTERVAL}秒")
    print(f"字体路径: {FONT_PATH}")
    print("="*50)
    
    # 启动摄像头处理线程
    camera_thread = threading.Thread(target=camera_processing, daemon=True)
    camera_thread.start()
    
    # 启动Flask应用
    app.run(host='0.0.0.0', port=5000, threaded=True)