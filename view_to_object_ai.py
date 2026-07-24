bl_info = {
    "name": "摄像机视图 AI 渲染图/视频生成器",
    "author": "AI Assistant",
    "version": (4, 6, 0),
    "blender": (3, 0, 0),
    "location": "3D视图 → 侧边栏(N键) → AI生成",
    "description": "渲染场景视图作为底图，调用 AI API 生成一张图片或一段视频，图片显示在图像编辑器、视频保存到本地",
    "category": "Object",
}

import bpy
import os
import base64
import json
import queue
import threading
import tempfile
import time
import random
import requests
from bpy.props import (
    StringProperty, BoolProperty, IntProperty, FloatProperty,
    PointerProperty, EnumProperty, CollectionProperty,
)
from bpy.types import Operator, Panel, PropertyGroup
import bpy.utils.previews

# =============================================================================
# 风格参考图缩略图预览集合（模块级单例）
# =============================================================================
_style_preview_collection = None

def _get_style_preview_collection():
    """获取/创建风格参考图的预览图标集合"""
    global _style_preview_collection
    if _style_preview_collection is None:
        _style_preview_collection = bpy.utils.previews.new()
    return _style_preview_collection


def _release_style_preview_collection():
    """释放风格参考图预览资源"""
    global _style_preview_collection
    if _style_preview_collection is not None:
        bpy.utils.previews.remove(_style_preview_collection)
        _style_preview_collection = None


# =============================================================================
# 模型下拉框枚举缓存（模块级，避免 EnumProperty 注册时访问 self 导致崩溃）
# =============================================================================
_image_model_enum_items = []   # [(identifier, name, description, icon), ...]
_video_model_enum_items = []


def _get_image_model_enum_items(self, context):
    """图像模型下拉选项（模块级函数，不访问 self 实例属性，注册安全）"""
    if not _image_model_enum_items:
        return [("(获取模型列表...)", "(尚未获取)", "点击「获取」按钮拉取模型列表", 0)]
    return _image_model_enum_items[:]


def _get_video_model_enum_items(self, context):
    """视频模型下拉选项（模块级函数，不访问 self 实例属性，注册安全）"""
    if not _video_model_enum_items:
        return [("(获取模型列表...)", "(尚未获取)", "点击「获取」按钮拉取模型列表", 0)]
    return _video_model_enum_items[:]


def _has_selected_image_model(props):
    """判断用户是否真的在下拉框选了一个图像模型（而非占位项）"""
    sel = (props.selected_image_model_id or "").strip()
    if not sel or sel == "(获取模型列表...)":
        return False
    return any(item[0] == sel for item in _image_model_enum_items)


def _has_selected_video_model(props):
    """判断用户是否真的在下拉框选了一个视频模型（而非占位项）"""
    sel = (props.selected_video_model_id or "").strip()
    if not sel or sel == "(获取模型列表...)":
        return False
    return any(item[0] == sel for item in _video_model_enum_items)


def _release_style_preview_collection():
    """插件卸载时释放预览资源"""
    global _style_preview_collection
    if _style_preview_collection is not None:
        bpy.utils.previews.remove(_style_preview_collection)
        _style_preview_collection = None


# =============================================================================
# 一、平台配置
# =============================================================================

PLATFORM_CHOICES = [
    ("sd_webui", "本地 SD WebUI",  "本地 Stable Diffusion WebUI 服务",  0),
    ("comfyui",  "本地 ComfyUI",   "本地 ComfyUI 服务（需指定工作流 JSON）",  1),
    ("api",      "各种 API",       "通用 fal-ai 兼容 API 转发站：输入网站+Token 后获取模型列表",  2),
]

SD_WEBUI_DEFAULT_URL = "http://127.0.0.1:7860/sdapi/v1/img2img"
COMFYUI_DEFAULT_URL = "http://127.0.0.1:8188"
API_DEFAULT_URL = "https://yunwu.ai"


def build_api_url_for_model(platform_url, model_id, platform="api"):
    """根据选中的模型 ID 自动构造完整 API URL"""
    base = platform_url.strip().rstrip('/')
    if not base.startswith(('http://', 'https://')):
        base = 'https://' + base
    if platform == "comfyui":
        return base
    if not model_id:
        return base
    if '/' in model_id:
        return f"{base}/{model_id.lstrip('/')}/image-to-image"
    else:
        return f"{base}/v1/images/edits"


def build_video_api_url(platform_url, model_id):
    """根据选中的视频模型 ID 构造视频生成 API URL

    视频模型多为 fal-ai 风格（含 / 的 owner/model 路径），
    转发站也可能直接提供 /v1/videos/generations 这类统一接口（model 在 body 中传递）。
    
    注意：无论哪种情况，调用时都应在 JSON body 中附带 "model" 字段，
    让后端知道实际要调用哪个模型。"""
    base = platform_url.strip().rstrip('/')
    if not base.startswith(('http://', 'https://')):
        base = 'https://' + base
    if not model_id:
        return base
    if '/' in model_id:
        # fal-ai 风格：https://base/owner/model  （model 已隐含在路径中）
        return f"{base}/{model_id.lstrip('/')}"
    # 统一端点，模型 ID 通过 body 的 "model" 字段传递
    return f"{base}/v1/videos/generations"


def _on_select_image_model(self, context):
    """用户在模型下拉框中自由选择后，自动按所选模型构造 API URL（不锁定默认项）"""
    if context is None:
        return
    mid = (self.selected_image_model_id or "").strip()
    if mid and self.image_platform_url.strip():
        # 仅当用户未手动改过 API URL（或 URL 与模型不匹配）时自动填充
        self.image_api_url = build_api_url_for_model(self.image_platform_url, mid, platform=self.image_platform)


def _on_select_video_model(self, context):
    """用户在视频模型下拉框中自由选择后，自动按所选模型构造视频 API URL。"""
    if context is None:
        return
    mid = (self.selected_video_model_id or "").strip()
    if mid and self.video_platform_url.strip():
        self.video_api_url = build_video_api_url(self.video_platform_url, mid)


def is_image_generation_model(model_id):
    """根据模型 ID 推测是否是图生图模型"""
    mid = model_id.lower()
    image_keywords = ["image", "img", "flux", "sd", "stable", "wan", "kandinsky",
                      "midjourney", "dall", "kontext", "sdxl", "dpo", "turbo", "lora", "vl", "edit"]
    text_keywords = ["gpt-3.5", "gpt-4", "gpt-4o", "claude", "gemini", "llama",
                    "deepseek", "mistral", "chatgpt", "embedding", "whisper",
                    "tts", "davinci", "babbage", "curie", "ada", "o1", "o3", "o4",
                    "chat", "instruct", "completion"]
    for kw in text_keywords:
        if kw in mid:
            return False, f"含文本关键词 {kw!r}"
    for kw in image_keywords:
        if kw in mid:
            return True, f"含图像关键词 {kw!r}"
    if "/" in model_id:
        return True, "含路径段"
    return False, "未识别"


def is_video_generation_model(model_id):
    """根据模型 ID 推测是否是视频生成模型（图生视频 / 文生视频）"""
    mid = model_id.lower()
    video_keywords = [
        "video", "vid", "kling", "luma", "minimax", "hunyuan", "sora",
        "seedance", "mochi", "gen-3", "gen3", "veo", "runway", "pixverse",
        "cogvideox", "animatediff", "wan", "i2v", "t2v", "dream-machine",
        "videocrafter", "open-sora", "svd", "stable-video",
    ]
    text_keywords = ["gpt-3.5", "gpt-4", "gpt-4o", "claude", "gemini", "llama",
                    "deepseek", "mistral", "chatgpt", "embedding", "whisper",
                    "tts", "davinci", "babbage", "curie", "ada", "o1", "o3", "o4",
                    "chat", "instruct", "completion"]
    for kw in text_keywords:
        if kw in mid:
            return False, f"含文本关键词 {kw!r}"
    for kw in video_keywords:
        if kw in mid:
            return True, f"含视频关键词 {kw!r}"
    return False, "未识别"


def is_gpt_or_google_model(model_id, owned_by=""):
    """判断是否是 GPT（OpenAI）或 Google 的图像模型"""
    mid = model_id.lower()
    owner = (owned_by or "").lower()
    is_gpt = (
        "gpt-image" in mid or "gpt_image" in mid or
        "dall-e" in mid or "dall_e" in mid or "dalle" in mid or
        "openai" in owner or
        mid.startswith("gpt-image") or mid.startswith("dall")
    )
    is_google = (
        "imagen" in mid or "google" in owner or
        "gemini-image" in mid or "gemini_image" in mid
    )
    return is_gpt or is_google, ("OpenAI" if is_gpt else ("Google" if is_google else ""))


def filter_models_list(models, filter_mode):
    """按过滤模式过滤模型列表"""
    if filter_mode == "all":
        return models
    if filter_mode == "all_image":
        return [m for m in models if is_image_generation_model(m["id"])[0]]
    if filter_mode == "video":
        return [m for m in models if is_video_generation_model(m["id"])[0]]
    filtered = []
    for m in models:
        is_img, _ = is_image_generation_model(m["id"])
        if not is_img:
            continue
        is_gg, _ = is_gpt_or_google_model(m["id"], m.get("owned_by", ""))
        if is_gg:
            filtered.append(m)
    return filtered


def _fetch_comfyui_models(platform_url):
    """从本地 ComfyUI 拉取可用 Checkpoint 列表（/object_info/CheckpointLoaderSimple）"""
    base = platform_url.strip().rstrip('/')
    if not base.startswith(('http://', 'https://')):
        base = 'https://' + base
    url = f"{base}/object_info/CheckpointLoaderSimple"
    try:
        resp = requests.get(url, timeout=20)
    except requests.exceptions.MissingSchema:
        return None, "URL 格式错误，需要 http:// 或 https:// 开头"
    except requests.exceptions.ConnectionError:
        return None, f"无法连接 {base}，请检查 ComfyUI 是否已启动"
    except requests.exceptions.Timeout:
        return None, "请求超时（>20s）"
    is_html, err = _detect_html(resp)
    if is_html:
        return None, err
    if resp.status_code == 404:
        return None, "未找到 /object_info/CheckpointLoaderSimple (HTTP 404)，请确认是 ComfyUI 地址"
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        data = resp.json()
    except ValueError:
        return None, f"返回非 JSON: {resp.text[:200]}"
    try:
        ckpt_list = data["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    except Exception:
        return None, f"ComfyUI 返回格式异常，无法解析模型列表: {str(data)[:200]}"
    models = []
    for name in ckpt_list:
        if isinstance(name, str):
            models.append({"id": name, "owned_by": "comfyui"})
    models.sort(key=lambda m: m["id"])
    return models, None


def fetch_models_from_api(platform_url, api_token, platform="api"):
    """从平台拉取可用模型列表（OpenAI 兼容 /v1/models 或 ComfyUI /object_info）"""
    if platform == "comfyui":
        return _fetch_comfyui_models(platform_url)

    base = platform_url.strip().rstrip('/')
    if not base.startswith(('http://', 'https://')):
        base = 'https://' + base
    url = f"{base}/v1/models"
    headers = {"Accept": "application/json"}
    if api_token.strip():
        headers["Authorization"] = f"Bearer {api_token.strip()}"
    try:
        resp = requests.get(url, headers=headers, timeout=20)
    except requests.exceptions.MissingSchema:
        return None, "URL 格式错误，需要 http:// 或 https:// 开头"
    except requests.exceptions.ConnectionError:
        return None, f"无法连接 {base}，请检查网站地址和网络"
    except requests.exceptions.Timeout:
        return None, "请求超时（>20s）"
    is_html, err = _detect_html(resp)
    if is_html:
        return None, err
    if resp.status_code in (401, 403):
        return None, f"鉴权失败 (HTTP {resp.status_code})，请检查 API Token"
    if resp.status_code == 404:
        return None, f"未找到 /v1/models 接口 (HTTP 404)，该网站可能不支持模型列表 API"
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        data = resp.json()
    except ValueError:
        return None, f"返回非 JSON: {resp.text[:200]}"
    if not isinstance(data, dict) or "data" not in data:
        return None, f"返回格式异常: {str(data)[:200]}"
    models = []
    for item in data["data"]:
        if isinstance(item, dict) and "id" in item:
            models.append({"id": str(item["id"]), "owned_by": str(item.get("owned_by", ""))})
    models.sort(key=lambda m: m["id"])
    return models, None


# =============================================================================
# 二、工具函数
# =============================================================================

def render_camera_view_to_temp(resolution_percentage=50):
    """自动渲染当前摄像机视图到临时文件，备份+还原渲染设置"""
    scene = bpy.context.scene
    camera = scene.camera
    if not camera:
        return None, "场景中没有摄像机，请先添加并设置为活动摄像机"

    orig = {
        "filepath":   scene.render.filepath,
        "format":     scene.render.image_settings.file_format,
        "res_x":      scene.render.resolution_x,
        "res_y":      scene.render.resolution_y,
        "percentage": scene.render.resolution_percentage,
    }

    tmp_path = os.path.join(tempfile.gettempdir(), "blender_ai_ref.png")
    scene.render.filepath = tmp_path
    scene.render.image_settings.file_format = 'PNG'
    scene.render.resolution_percentage = resolution_percentage

    try:
        bpy.ops.render.render(write_still=True)
    except Exception as e:
        return None, f"渲染失败: {e}"
    finally:
        scene.render.filepath = orig["filepath"]
        scene.render.image_settings.file_format = orig["format"]
        scene.render.resolution_x = orig["res_x"]
        scene.render.resolution_y = orig["res_y"]
        scene.render.resolution_percentage = orig["percentage"]

    if not os.path.exists(tmp_path):
        return None, "渲染输出文件未生成"
    return tmp_path, None


def render_camera_animation_to_temp(resolution_percentage=35):
    """渲染当前场景的摄像机动画到临时 mp4，作为视频生成的镜头运动参考。

    仅渲染场景自身的动画（frame_start..frame_end），备份并还原渲染设置。
    生成的视频通过 data URL 传给视频生成 API（video-to-video / 运动参考）。
    """
    scene = bpy.context.scene
    camera = scene.camera
    if not camera:
        return None, "场景中没有摄像机，无法渲染镜头动画"
    frame_start = scene.frame_start
    frame_end = scene.frame_end
    if frame_end <= frame_start:
        return None, "场景没有动画帧范围（frame_start >= frame_end），无法作为镜头参考"

    orig = {
        "filepath":        scene.render.filepath,
        "format":          scene.render.image_settings.file_format,
        "ffmpeg_format":   scene.render.ffmpeg.format,
        "ffmpeg_codec":    scene.render.ffmpeg.codec,
        "ffmpeg_audio":    scene.render.ffmpeg.audio_codec,
        "res_x":           scene.render.resolution_x,
        "res_y":           scene.render.resolution_y,
        "percentage":      scene.render.resolution_percentage,
        "fps":             scene.render.fps,
        "frame_current":   scene.frame_current,
    }

    tmp_path = os.path.join(tempfile.gettempdir(), "blender_ai_cam_anim.mp4")
    # 若旧文件存在先删除，避免被覆盖逻辑忽略
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except Exception:
        pass

    scene.render.filepath = tmp_path
    scene.render.image_settings.file_format = 'FFMPEG'
    scene.render.ffmpeg.format = 'MPEG4'
    scene.render.ffmpeg.codec = 'H264'
    scene.render.ffmpeg.audio_codec = 'NONE'
    scene.render.resolution_percentage = resolution_percentage

    try:
        bpy.ops.render.render(animation=True, write_still=False)
    except Exception as e:
        return None, f"镜头动画渲染失败: {e}"
    finally:
        scene.render.filepath = orig["filepath"]
        scene.render.image_settings.file_format = orig["format"]
        scene.render.ffmpeg.format = orig["ffmpeg_format"]
        scene.render.ffmpeg.codec = orig["ffmpeg_codec"]
        scene.render.ffmpeg.audio_codec = orig["ffmpeg_audio"]
        scene.render.resolution_x = orig["res_x"]
        scene.render.resolution_y = orig["res_y"]
        scene.render.resolution_percentage = orig["percentage"]
        scene.render.fps = orig["fps"]
        scene.frame_current = orig["frame_current"]

    if not os.path.exists(tmp_path):
        return None, "镜头动画视频未生成"
    return tmp_path, None


def render_camera_first_last_frames(resolution_percentage=50):
    """渲染相机动画的第一帧和最后一帧为两张临时 PNG，用于首尾帧视频生成。

    首帧=frame_start，尾帧=frame_end；返回 (first_path, last_path, err)。
    """
    scene = bpy.context.scene
    camera = scene.camera
    if not camera:
        return None, None, "场景中没有摄像机，无法渲染首尾帧"
    frame_start = scene.frame_start
    frame_end = scene.frame_end
    if frame_end < frame_start:
        frame_end = frame_start

    orig = {
        "filepath":      scene.render.filepath,
        "format":        scene.render.image_settings.file_format,
        "res_x":         scene.render.resolution_x,
        "res_y":         scene.render.resolution_y,
        "percentage":    scene.render.resolution_percentage,
        "frame_current": scene.frame_current,
    }

    first_path = os.path.join(tempfile.gettempdir(), "blender_ai_first_frame.png")
    last_path = os.path.join(tempfile.gettempdir(), "blender_ai_last_frame.png")

    def _render_one(target_path, frame):
        scene.render.filepath = target_path
        scene.render.image_settings.file_format = 'PNG'
        scene.render.resolution_percentage = resolution_percentage
        scene.frame_set(frame)
        try:
            bpy.ops.render.render(write_still=True)
        except Exception as e:
            return f"渲染第 {frame} 帧失败: {e}"
        if not os.path.exists(target_path):
            return f"第 {frame} 帧输出文件未生成"
        return None

    err = None
    try:
        err = _render_one(first_path, frame_start)
        if not err:
            err = _render_one(last_path, frame_end)
    finally:
        scene.render.filepath = orig["filepath"]
        scene.render.image_settings.file_format = orig["format"]
        scene.render.resolution_x = orig["res_x"]
        scene.render.resolution_y = orig["res_y"]
        scene.render.resolution_percentage = orig["percentage"]
        scene.frame_set(orig["frame_current"])

    if err:
        return None, None, err
    return first_path, last_path, None


def image_to_base64(image_path):
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode('utf-8')


def image_to_data_url(image_path):
    """把图片转成 data URL，自动识别 MIME（JPEG 比 PNG 小很多，省上下文）"""
    ext = os.path.splitext(image_path)[1].lower()
    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
    b64 = image_to_base64(image_path)
    return f"data:{mime};base64,{b64}"


def file_to_data_url(file_path, mime=None):
    """把任意文件（图片/视频）转成 data URL，用于作为 API 的参考输入"""
    ext = os.path.splitext(file_path)[1].lower()
    if mime is None:
        mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
        }.get(ext, "application/octet-stream")
    b64 = image_to_base64(file_path)
    return f"data:{mime};base64,{b64}"


def prepare_reference_image(src_path, max_size=1024, quality=85):
    """优化参考图：按比例缩放到最大边长，并转存为 JPEG。

    发送给图生图 API 的图片体积是上下文/带宽/费用的主要消耗点，
    PNG 无损体积大，缩放 + JPEG 可显著压缩。失败则回退原图。"""
    try:
        img = bpy.data.images.load(src_path)
    except Exception:
        return src_path
    try:
        w, h = img.size
        max_dim = max(w, h)
        out_path = os.path.join(tempfile.gettempdir(), "blender_ai_ref_opt.jpg")
        if max_size and max_dim > max_size:
            scale = max_size / float(max_dim)
            nw = max(1, int(round(w * scale)))
            nh = max(1, int(round(h * scale)))
            img.scale(nw, nh)
        img.file_format = 'JPEG'
        try:
            img.quality = int(quality)
        except Exception:
            pass
        img.save_render(out_path)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception as e:
        print(f"[AI_Gen] 参考图优化失败，使用原图: {e}")
    finally:
        try:
            bpy.data.images.remove(img)
        except Exception:
            pass
    return src_path


def base64_to_temp_image(b64_str, suffix=".png"):
    img_data = base64.b64decode(b64_str)
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(img_data)
    tmp.close()
    return tmp.name


def save_generated_image(image_path, prefix="ai_gen"):
    """把生成的图片保存到用户文档目录下，方便用户取用"""
    try:
        save_dir = os.path.join(os.path.expanduser("~"), "AI_Generated_Images")
        os.makedirs(save_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(save_dir, f"{prefix}_{timestamp}.png")
        with open(image_path, "rb") as fs, open(dst, "wb") as fd:
            fd.write(fs.read())
        return dst
    except Exception as e:
        print(f"[AI_Gen] 保存失败: {e}")
        return None


# =============================================================================
# 三、API 调用
# =============================================================================

def _detect_html(resp):
    """检测响应是否为 HTML 页面"""
    content_type = resp.headers.get('Content-Type', '').lower()
    text = resp.text[:500] if hasattr(resp, 'text') else ""
    if 'html' in content_type or text.lstrip().lower().startswith(('<!doctype', '<html')):
        status = resp.status_code
        if status in (401, 403):
            cause = f"HTTP {status} 鉴权失败 — Token 无效或缺失"
            suggestion = "请检查 API Token"
        elif status == 404:
            cause = f"HTTP 404 接口不存在 — URL 路径错误"
            suggestion = "请在模型列表中选含 / 的 fal-ai 风格模型"
        elif status in (301, 302, 303, 307, 308):
            cause = f"HTTP {status} 重定向 — URL 不对"
            suggestion = "请检查 URL 路径"
        elif status == 200:
            cause = "HTTP 200 但返回 HTML"
            suggestion = "URL 可能是网站根域名"
        else:
            cause = f"HTTP {status} 异常"
            suggestion = "请检查 URL 和 Token"
        snippet = text[:200].replace('\n', ' ').replace('\r', ' ')
        return True, f"{cause}。{suggestion}。片段: {snippet}"
    return False, None


def _call_sd_webui(api_url, api_token, b64_img, prompt, negative_prompt,
                   denoising, steps, cfg_scale, image_size="1024x1024",
                   style_ref_image_path=None):
    """SD WebUI 图生图协议"""
    w, h = 512, 512
    s = str(image_size)
    if "x" in s:
        try:
            w, h = s.split("x")
            w, h = int(w), int(h)
        except Exception:
            w, h = 512, 512
    elif s.isdigit():
        w = h = int(s)
    payload = {
        "init_images": [b64_img],
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "denoising_strength": float(denoising),
        "steps": int(steps),
        "cfg_scale": float(cfg_scale),
        "width": w,
        "height": h,
    }
    # SD WebUI img2img 原生只吃一张 init 图；风格参考图通过附加 alwayson_scripts
    # 不易通用，这里把风格参考图以 base64 形式带出，便于转发站按需转 ControlNet/Style。
    if style_ref_image_path and os.path.exists(style_ref_image_path):
        try:
            payload["style_reference_image"] = image_to_base64(style_ref_image_path)
        except Exception:
            pass
    headers = {"Content-Type": "application/json"}
    if api_token.strip():
        headers["Authorization"] = f"Bearer {api_token.strip()}"
    resp = requests.post(api_url, json=payload, headers=headers, timeout=180)
    is_html, err = _detect_html(resp)
    if is_html:
        return {"_error": err}
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return {"_error": f"API 返回非 JSON 响应: {resp.text[:200]}"}


def _call_fal(api_url, api_token, data_url, prompt, negative_prompt,
              denoising, steps, cfg_scale, image_size="", style_ref_image_path=None):
    """fal-ai 协议：JSON body + image_url（data_url 已含正确 MIME）"""
    payload = {
        "image_url":    data_url,
        "image_base64": data_url,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "strength": float(denoising),
        "num_inference_steps": int(steps),
        "guidance_scale": float(cfg_scale),
        "image_format": "png",
        "num_images": 1,
    }
    if style_ref_image_path and os.path.exists(style_ref_image_path):
        try:
            style_url = image_to_data_url(style_ref_image_path)
            payload["reference_image_url"] = style_url
            payload["style_image_url"] = style_url
        except Exception:
            pass
    if image_size:
        s = str(image_size)
        if "x" in s:
            payload["image_size"] = s
            try:
                aw, ah = s.split("x")
                payload["aspect_ratio"] = f"{int(aw)}:{int(ah)}"
            except Exception:
                pass
        elif s.isdigit():
            payload["image_size"] = f"{s}x{s}"
            payload["aspect_ratio"] = "1:1"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_token.strip():
        headers["Authorization"] = f"Bearer {api_token.strip()}"
    resp = requests.post(api_url, json=payload, headers=headers, timeout=180)
    is_html, err = _detect_html(resp)
    if is_html:
        return {"_error": err}
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return {"_error": f"API 返回非 JSON 响应: {resp.text[:200]}"}


def _call_openai_image_edit(api_url, api_token, ref_image_path, prompt,
                             negative_prompt, model_id="gpt-image-2",
                             image_size="1024x1024", style_ref_image_path=None):
    """OpenAI Images Edit 协议：multipart/form-data"""
    # OpenAI 仅支持 1024x1024 / 1536x1024 / 1024x1536，按横竖取最接近
    allowed = {"1024x1024", "1536x1024", "1024x1536"}
    s = str(image_size)
    if s not in allowed:
        try:
            w, h = s.split("x")
            w, h = int(w), int(h)
            s = "1536x1024" if w >= h else "1024x1536"
        except Exception:
            s = "1024x1024"
    headers = {}
    if api_token.strip():
        headers["Authorization"] = f"Bearer {api_token.strip()}"
    # 打开主参考图 + 风格参考图（如有），请求期间保持文件句柄打开
    handles = []
    try:
        ext = os.path.splitext(ref_image_path)[1].lower()
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
        fname = f"ref{ext}" if ext else "ref.png"
        fh = open(ref_image_path, "rb")
        handles.append(fh)
        files = {"image": (fname, fh, mime)}
        if style_ref_image_path and os.path.exists(style_ref_image_path):
            sext = os.path.splitext(style_ref_image_path)[1].lower()
            smime = "image/jpeg" if sext in (".jpg", ".jpeg") else "image/png"
            sfname = f"style{sext}" if sext else "style.png"
            sfh = open(style_ref_image_path, "rb")
            handles.append(sfh)
            files["style_image"] = (sfname, sfh, smime)
        data = {
            "prompt": prompt,
            "model": model_id or "gpt-image-2",
            "size": s,
            "n": "1",
            "response_format": "b64_json",
        }
        resp = requests.post(api_url, headers=headers, files=files, data=data, timeout=180)
    except Exception as e:
        return {"_error": f"读取参考图失败: {e}"}
    finally:
        for fh in handles:
            try:
                fh.close()
            except Exception:
                pass
    is_html, err = _detect_html(resp)
    if is_html:
        return {"_error": err}
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return {"_error": f"API 返回非 JSON 响应: {resp.text[:200]}"}


def _call_comfyui(platform_url, workflow_file, ref_image_path, prompt, negative_prompt,
                  model_id="", image_size="1024x1024", denoising=0.75, steps=20, cfg_scale=7.0,
                  style_ref_image_path=None):
    """ComfyUI 协议：加载用户工作流 JSON，替换占位符，上传参考图，提交 /prompt 并轮询 /history"""
    base = platform_url.strip().rstrip('/')
    if not base.startswith(('http://', 'https://')):
        base = 'https://' + base

    if not workflow_file or not os.path.exists(workflow_file):
        return {"_error": "请先选择 ComfyUI 工作流 JSON 文件（右键工作流→保存(API 格式)）"}

    try:
        with open(workflow_file, 'r', encoding='utf-8') as f:
            workflow = json.load(f)
    except Exception as e:
        return {"_error": f"读取工作流文件失败: {e}"}

    w, h = 1024, 1024
    s = str(image_size)
    if "x" in s:
        try:
            w, h = s.split("x")
            w, h = int(w), int(h)
        except Exception:
            w, h = 1024, 1024
    elif s.isdigit():
        w = h = int(s)

    seed = random.randint(1, 2**32 - 1)
    placeholders = {
        "{prompt}": prompt,
        "{negative_prompt}": negative_prompt,
        "{width}": str(w),
        "{height}": str(h),
        "{seed}": str(seed),
        "{steps}": str(int(steps)),
        "{cfg}": str(float(cfg_scale)),
        "{denoising}": str(float(denoising)),
        "{sampler_name}": "euler",
        "{scheduler}": "normal",
        "{ckpt_name}": model_id or "",
    }

    def _has_placeholder(ph):
        return ph in json.dumps(workflow)

    # 上传主参考图（工作流里出现 {input_image} 占位符时）
    if _has_placeholder("{input_image}"):
        if not ref_image_path or not os.path.exists(ref_image_path):
            return {"_error": "工作流需要 {input_image}，但未提供参考图"}
        try:
            ext = os.path.splitext(ref_image_path)[1].lower() or ".png"
            mime = "image/png" if ext == ".png" else "image/jpeg"
            fname = f"blender_ai_ref{ext}"
            with open(ref_image_path, "rb") as fh:
                resp = requests.post(
                    f"{base}/upload/image",
                    files={"image": (fname, fh, mime)},
                    data={"overwrite": "true"},
                    timeout=60,
                )
            resp.raise_for_status()
            upload_result = resp.json()
            uploaded_name = upload_result.get("name") or upload_result.get("filename") or fname
            placeholders["{input_image}"] = uploaded_name
        except Exception as e:
            return {"_error": f"上传参考图到 ComfyUI 失败: {e}"}

    # 上传风格参考图（工作流里出现 {style_image} 占位符时）
    if _has_placeholder("{style_image}"):
        if style_ref_image_path and os.path.exists(style_ref_image_path):
            try:
                ext = os.path.splitext(style_ref_image_path)[1].lower() or ".png"
                mime = "image/png" if ext == ".png" else "image/jpeg"
                fname = f"blender_ai_style{ext}"
                with open(style_ref_image_path, "rb") as fh:
                    resp = requests.post(
                        f"{base}/upload/image",
                        files={"image": (fname, fh, mime)},
                        data={"overwrite": "true"},
                        timeout=60,
                    )
                resp.raise_for_status()
                upload_result = resp.json()
                uploaded_name = upload_result.get("name") or upload_result.get("filename") or fname
                placeholders["{style_image}"] = uploaded_name
            except Exception as e:
                placeholders["{style_image}"] = ""
        else:
            placeholders["{style_image}"] = ""

    def _replace_placeholders(obj):
        if isinstance(obj, dict):
            return {k: _replace_placeholders(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_replace_placeholders(v) for v in obj]
        if isinstance(obj, str):
            for ph, val in placeholders.items():
                obj = obj.replace(ph, val)
            return obj
        return obj

    # 若工作流用了 {ckpt_name} 占位符但没选模型，提前给出明确提示，避免提交到 ComfyUI 因空模型名失败
    if _has_placeholder("{ckpt_name}") and not (model_id or "").strip():
        return {"_error": "工作流包含 {ckpt_name} 占位符但未选择模型。请在 ComfyUI 工作流里把 CheckpointLoader 节点写死模型名，或点「获取」选择 checkpoint 后再生成"}

    workflow = _replace_placeholders(workflow)

    # 关键占位符替换检查：若替换后仍残留，说明工作流写入方式/节点类型异常
    workflow_str = json.dumps(workflow)
    for ph in ("{prompt}", "{negative_prompt}", "{input_image}", "{style_image}", "{ckpt_name}"):
        if ph in workflow_str:
            return {"_error": f"工作流占位符未替换: {ph}。请在对应节点写入占位符，不要使用下拉选择/固定值覆盖"}

    prompt_id = None
    try:
        resp = requests.post(f"{base}/prompt", json={"prompt": workflow, "client_id": "view_to_object_ai"}, timeout=60)
        resp.raise_for_status()
        prompt_data = resp.json()
        prompt_id = prompt_data.get("prompt_id")
        if not prompt_id:
            return {"_error": f"ComfyUI /prompt 未返回 prompt_id: {resp.text[:200]}"}
    except Exception as e:
        return {"_error": f"提交 ComfyUI 工作流失败: {e}"}

    def _dump_debug(extra=None):
        try:
            dump_path = os.path.join(tempfile.gettempdir(), "view_to_object_ai_comfyui_debug.json")
            info = {"prompt_id": prompt_id, "base": base, "last_info": last_info}
            if extra is not None:
                info.update(extra)
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    history_url = f"{base}/history/{prompt_id}"
    last_info = ""
    last_data = None
    # 轮询上限约 30 分钟：前 60 次 2 秒（2 分钟快速等待），之后 5 秒一次
    for i in range(540):
        if i < 60:
            time.sleep(2)
        else:
            time.sleep(5)
        try:
            resp = requests.get(history_url, timeout=30)
            if resp.status_code != 200:
                last_info = f"HTTP {resp.status_code}"
                continue
            data = resp.json()
            last_data = data
            if not isinstance(data, dict):
                continue
            job = data.get(prompt_id)
            if not job:
                continue
            outputs = job.get("outputs", {})
            if not outputs:
                continue
            for node_id, node_out in outputs.items():
                if not isinstance(node_out, dict):
                    continue
                images = node_out.get("images", [])
                for img_info in images:
                    if not isinstance(img_info, dict):
                        continue
                    filename = img_info.get("filename")
                    if not filename:
                        continue
                    # 跳过 LoadImage 等节点的「输入回显」(type=input)；
                    # 其余（SaveImage/PreviewImage，含无 type 字段的版本）一律视为输出图下载
                    if img_info.get("type") == "input":
                        continue
                    subfolder = img_info.get("subfolder", "")
                    view_url = f"{base}/view?filename={requests.utils.quote(filename)}"
                    if subfolder:
                        view_url += f"&subfolder={requests.utils.quote(subfolder)}"
                    view_url += "&type=output"
                    try:
                        img_resp = requests.get(view_url, timeout=60)
                        img_resp.raise_for_status()
                        suffix = os.path.splitext(filename)[1].lower() or ".png"
                        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
                        tmp.write(img_resp.content)
                        tmp.close()
                        _dump_debug({"status": "success", "filename": filename, "outputs_keys": list(outputs.keys())})
                        return {"_local_path": tmp.name}
                    except Exception as e:
                        _dump_debug({"status": "download_error", "error": str(e), "filename": filename})
                        return {"_error": f"下载 ComfyUI 输出图失败: {e}"}
            last_info = "outputs 中无图片"
        except Exception as e:
            last_info = str(e)

    _dump_debug({"status": "timeout", "last_data": last_data})
    return {"_error": f"ComfyUI 任务轮询超时。最后状态: {last_info}"}


def _upload_comfyui_image(base, image_path, prefix="blender"):
    """上传单张图片到 ComfyUI /upload/image，返回服务端文件名"""
    ext = os.path.splitext(image_path)[1].lower() or ".png"
    mime = "image/png" if ext == ".png" else "image/jpeg"
    fname = f"{prefix}{ext}"
    with open(image_path, "rb") as fh:
        resp = requests.post(
            f"{base}/upload/image",
            files={"image": (fname, fh, mime)},
            data={"overwrite": "true"},
            timeout=60,
        )
    resp.raise_for_status()
    up = resp.json()
    return up.get("name") or up.get("filename") or fname


def _comfyui_download(base, filename, subfolder, ftype="output"):
    """从 ComfyUI /view 下载输出文件到本地临时文件，返回路径"""
    view_url = f"{base}/view?filename={requests.utils.quote(filename)}"
    if subfolder:
        view_url += f"&subfolder={requests.utils.quote(subfolder)}"
    view_url += f"&type={ftype}"
    resp = requests.get(view_url, timeout=120)
    resp.raise_for_status()
    suffix = os.path.splitext(filename)[1].lower() or ".mp4"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(resp.content)
    tmp.close()
    return tmp.name


def _call_comfyui_video(platform_url, workflow_file, prompt, negative_prompt="",
                        ref_video_path=None, first_frame_path=None, last_frame_path=None,
                        ref_image_path=None, model_id="", width=1280, height=720,
                        duration=4.0, seed=None):
    """ComfyUI 视频协议：加载视频工作流 JSON，替换占位符，上传参考素材，提交 /prompt 并轮询 /history 下载视频。

    工作流需用 ComfyUI 右键→保存(API 格式)，并在对应节点写入以下占位符（缺哪个就不替换哪个）：
      {prompt}        正向提示词（建议放到 CLIPTextEncode 的 text 里）
      {negative_prompt} 反向提示词
      {ckpt_name}     CheckpointLoaderSimple 的 ckpt_name（也可在「视频模型」下拉选 checkpoint 自动注入）
      {seed}         随机种子
      {width}/{height} 输出宽高（用于 EmptyLatentImage / VAEEncode 等）
      {duration}/{frames}/{fps} 时长 / 帧数 / 帧率
      {input_video}/{ref_video}/{motion_video} 镜头运动参考视频（本地 mp4，自动上传到 /upload/video）
      {first_frame}/{start_image}/{first_image} 首帧（自动上传）
      {last_frame}/{end_image}/{last_image}    尾帧（自动上传）
      {input_image}/{image}                    参考图（图生视频起点，自动上传）
    """
    base = platform_url.strip().rstrip('/')
    if not base.startswith(('http://', 'https://')):
        base = 'https://' + base

    if not workflow_file or not os.path.exists(workflow_file):
        return {"_error": "请先在上方选择 ComfyUI 视频工作流 JSON（右键工作流→保存(API 格式)）"}
    try:
        with open(workflow_file, 'r', encoding='utf-8') as f:
            workflow = json.load(f)
    except Exception as e:
        return {"_error": f"读取工作流文件失败: {e}"}

    if seed is None:
        seed = random.randint(1, 2**32 - 1)
    placeholders = {
        "{prompt}": prompt,
        "{negative_prompt}": negative_prompt,
        "{seed}": str(seed),
        "{width}": str(int(width)),
        "{height}": str(int(height)),
        "{ckpt_name}": model_id or "",
        "{duration}": str(duration),
        "{frames}": str(int(round(float(duration) * 24))),
        "{fps}": "24",
    }
    dump = json.dumps(workflow)

    def _has(ph):
        return ph in dump

    # 上传镜头运动参考视频（motion 模式）
    for vph in ("{input_video}", "{ref_video}", "{motion_video}"):
        if _has(vph):
            if ref_video_path and os.path.exists(ref_video_path):
                try:
                    ext = os.path.splitext(ref_video_path)[1].lower() or ".mp4"
                    fname = f"blender_ai_ref{ext}"
                    with open(ref_video_path, "rb") as fh:
                        resp = requests.post(
                            f"{base}/upload/video",
                            files={"video": (fname, fh, "video/mp4")},
                            data={"overwrite": "true"},
                            timeout=180,
                        )
                    resp.raise_for_status()
                    up = resp.json()
                    uploaded = up.get("name") or up.get("filename") or fname
                    placeholders[vph] = uploaded
                except Exception as e:
                    return {"_error": f"上传参考视频到 ComfyUI 失败（需 ComfyUI 支持 /upload/video，较新版本才有）: {e}"}
            break

    # 上传首帧
    for iph in ("{first_frame}", "{start_image}", "{first_image}"):
        if _has(iph) and first_frame_path and os.path.exists(first_frame_path):
            try:
                placeholders[iph] = _upload_comfyui_image(base, first_frame_path, "blender_first")
            except Exception as e:
                return {"_error": f"上传首帧到 ComfyUI 失败: {e}"}
            break

    # 上传尾帧
    for iph in ("{last_frame}", "{end_image}", "{last_image}"):
        if _has(iph) and last_frame_path and os.path.exists(last_frame_path):
            try:
                placeholders[iph] = _upload_comfyui_image(base, last_frame_path, "blender_last")
            except Exception as e:
                return {"_error": f"上传尾帧到 ComfyUI 失败: {e}"}
            break

    # 上传参考图（图生视频起点）
    for iph in ("{input_image}", "{image}"):
        if _has(iph) and ref_image_path and os.path.exists(ref_image_path):
            try:
                placeholders[iph] = _upload_comfyui_image(base, ref_image_path, "blender_ref")
            except Exception as e:
                return {"_error": f"上传参考图到 ComfyUI 失败: {e}"}
            break

    def _replace(obj):
        if isinstance(obj, dict):
            return {k: _replace(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_replace(v) for v in obj]
        if isinstance(obj, str):
            for ph, val in placeholders.items():
                if ph in obj:
                    obj = obj.replace(ph, val)
            return obj
        return obj

    workflow = _replace(workflow)

    try:
        resp = requests.post(f"{base}/prompt", json={"prompt": workflow, "client_id": "view_to_object_ai"}, timeout=60)
        resp.raise_for_status()
        prompt_data = resp.json()
        prompt_id = prompt_data.get("prompt_id")
        if not prompt_id:
            return {"_error": f"ComfyUI /prompt 未返回 prompt_id: {resp.text[:200]}"}
    except Exception as e:
        return {"_error": f"提交 ComfyUI 视频工作流失败: {e}"}

    history_url = f"{base}/history/{prompt_id}"
    last_info = ""
    for _ in range(300):  # 300 * 2s = 10 分钟
        time.sleep(2)
        try:
            resp = requests.get(history_url, timeout=30)
            if resp.status_code != 200:
                last_info = f"HTTP {resp.status_code}"
                continue
            data = resp.json()
            if not isinstance(data, dict):
                continue
            job = data.get(prompt_id)
            if not job:
                continue
            outputs = job.get("outputs", {})
            if not outputs:
                continue
            for node_out in outputs.values():
                if not isinstance(node_out, dict):
                    continue
                # 视频输出：新版 videos / gifs，或 images 含视频扩展名
                for key in ("videos", "gifs"):
                    for v in node_out.get(key, []):
                        if isinstance(v, dict) and v.get("filename"):
                            return {"_local_path": _comfyui_download(
                                base, v["filename"], v.get("subfolder", ""), v.get("type", "output"))}
                for img in node_out.get("images", []):
                    if isinstance(img, dict) and img.get("filename"):
                        fn = img["filename"].lower()
                        if fn.endswith((".mp4", ".webm", ".mov", ".mkv", ".gif")):
                            return {"_local_path": _comfyui_download(
                                base, img["filename"], img.get("subfolder", ""), img.get("type", "output"))}
            last_info = "outputs 中尚无视频文件"
        except Exception as e:
            last_info = str(e)

    return {"_error": f"ComfyUI 视频任务轮询超时（10 分钟）。最后状态: {last_info}"}


def _parse_image_response(result):
    """统一解析多种返回格式"""
    if isinstance(result, dict) and "_error" in result:
        return None, result["_error"]
    if isinstance(result, dict) and "_local_path" in result:
        path = result["_local_path"]
        if os.path.exists(path):
            return path, None
        return None, f"本地输出文件不存在: {path}"
    # SD WebUI
    if "images" in result and isinstance(result["images"], list) and result["images"]:
        img_data = result["images"][0]
        if isinstance(img_data, dict) and "b64_json" in img_data:
            img_data = img_data["b64_json"]
        if isinstance(img_data, str):
            if img_data.startswith("data:image"):
                img_data = img_data.split(",", 1)[1]
            return base64_to_temp_image(img_data), None
    # OpenAI
    if "data" in result and isinstance(result["data"], list) and result["data"]:
        item = result["data"][0]
        if isinstance(item, dict):
            if "b64_json" in item:
                return base64_to_temp_image(item["b64_json"]), None
            if "url" in item:
                return _download_to_temp(item["url"])
    # fal-ai
    if "output" in result:
        out = result["output"]
        if isinstance(out, dict):
            if "images" in out and out["images"]:
                img_data = out["images"][0]
                if isinstance(img_data, str):
                    if img_data.startswith("data:image"):
                        img_data = img_data.split(",", 1)[1]
                    return base64_to_temp_image(img_data), None
                if isinstance(img_data, dict) and "url" in img_data:
                    return _download_to_temp(img_data["url"])
            if "image" in out and isinstance(out["image"], str):
                img_data = out["image"]
                if img_data.startswith("data:image"):
                    img_data = img_data.split(",", 1)[1]
                return base64_to_temp_image(img_data), None
            if "url" in out and isinstance(out["url"], str):
                return _download_to_temp(out["url"])
        elif isinstance(out, str):
            if out.startswith("data:image"):
                return base64_to_temp_image(out.split(",", 1)[1]), None
            elif out.startswith("http"):
                return _download_to_temp(out)
    # Replicate
    if "urls" in result and isinstance(result["urls"], dict) and "get" in result["urls"]:
        return _download_to_temp(result["urls"]["get"])
    if "url" in result and isinstance(result["url"], str) and result["url"].startswith("http"):
        return _download_to_temp(result["url"])
    return None, f"API 返回格式异常，响应片段: {str(result)[:300]}"


def _download_to_temp(url, suffix=".png"):
    try:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.write(r.content)
        tmp.close()
        return tmp.name, None
    except Exception as e:
        return None, f"下载生成文件失败: {e}"


def _detect_protocol(platform, api_url, model_id=""):
    """根据平台、URL、模型 ID 智能判断协议"""
    if platform == "sd_webui":
        return "sd_webui"
    if platform == "comfyui":
        return "comfyui"
    url_lower = api_url.lower()
    if "/v1/images/edits" in url_lower or "/v1/images/generations" in url_lower:
        return "openai"
    if model_id and "/" not in model_id:
        return "openai"
    return "fal"


def call_api(platform, api_url, api_token, ref_image_path, prompt,
             negative_prompt, retry=2, model_id="", image_size="1024",
             denoising=0.75, steps=20, cfg_scale=7.0, style_ref_image_path=None,
             platform_url="", workflow_file=""):
    """统一 API 入口"""
    protocol = _detect_protocol(platform, api_url, model_id)
    last_err = None
    for attempt in range(retry + 1):
        try:
            if protocol == "sd_webui":
                b64 = image_to_base64(ref_image_path)
                result = _call_sd_webui(api_url, api_token, b64, prompt,
                                        negative_prompt, denoising, steps, cfg_scale,
                                        style_ref_image_path=style_ref_image_path)
            elif protocol == "comfyui":
                comfyui_base = platform_url.strip() or api_url.strip()
                result = _call_comfyui(comfyui_base, workflow_file, ref_image_path, prompt,
                                       negative_prompt, model_id=model_id, image_size=image_size,
                                       denoising=denoising, steps=steps, cfg_scale=cfg_scale,
                                       style_ref_image_path=style_ref_image_path)
            elif protocol == "openai":
                size_str = f"{image_size}x{image_size}" if image_size and str(image_size).isdigit() else "1024x1024"
                result = _call_openai_image_edit(api_url, api_token, ref_image_path, prompt,
                                                  negative_prompt, model_id=model_id, image_size=size_str,
                                                  style_ref_image_path=style_ref_image_path)
            else:
                data_url = image_to_data_url(ref_image_path)
                result = _call_fal(api_url, api_token, data_url, prompt,
                                   negative_prompt, denoising, steps, cfg_scale, image_size,
                                   style_ref_image_path=style_ref_image_path)
            return _parse_image_response(result)
        except requests.exceptions.MissingSchema:
            return None, "API 地址格式错误"
        except requests.exceptions.ConnectionError:
            last_err = "无法连接 API 地址"
        except requests.exceptions.HTTPError as e:
            resp = e.response
            err_text = resp.text[:300] if resp is not None else str(e)
            last_err = f"HTTP {resp.status_code if resp is not None else '?'}: {err_text}"
            if resp is not None and 400 <= resp.status_code < 500:
                if resp.status_code in (401, 403):
                    last_err = f"API 鉴权失败 (HTTP {resp.status_code})"
                return None, last_err
        except requests.exceptions.Timeout:
            last_err = "API 请求超时（>180s）"
        except Exception as e:
            last_err = f"API 调用失败: {e}"
        if attempt < retry:
            time.sleep(1.5 * (attempt + 1))
    return None, last_err or "API 调用失败"


# =============================================================================
# 3.5、视频生成 API 调用（图生视频 / 文生视频）
# =============================================================================

def _call_video_api(api_url, api_token, ref_image_path, prompt,
                    duration=4.0, fps=24, aspect_ratio="16:9", image_size="1280x720",
                    style_ref_image_path=None, ref_video_path=None, model_id="",
                    ref_image_url="", style_ref_url="", ref_video_file="", ref_video_url="",
                    first_frame_path=None, last_frame_path=None):
    """调用视频生成模型：把参考图 + 提示词发给 fal-ai 兼容的视频接口。

    说明：
    - 采用 JSON body（image_url + prompt + 时长/帧率等），兼容多数转发站与 fal-ai 视频模型。
    - model_id 会放入 payload 的 "model" 字段，让后端知道要调哪个模型（关键！很多 404 就是因为缺这个）。
    - 可选 ref_video_path：把 Blender 渲染的镜头动画视频作为运动参考（video-to-video）。
    - fal-ai 真实服务是异步队列：POST 可能只返回 request_id，需要轮询 GET /requests/{id}
      直到 COMPLETED，再从 .response 中取出视频。
    - 视频体积大、生成慢，超时放宽到 600s，轮询上限约 10 分钟。
    """
    payload = {
        "model":            model_id or "",
        "prompt":           prompt,
        # 同时给出多种常见字段名，模型忽略不认识的即可
        # duration 必须为整数（部分中转站后端用 Go struct int 类型，不接受 4.0 这类浮点）
        "duration":         int(duration),
        "duration_seconds": int(duration),
        "fps":              int(fps),
        "num_frames":       max(1, int(round(float(duration) * int(fps)))),
        "aspect_ratio":     aspect_ratio,
        "size":             image_size,
    }
    # 主参考图：优先「已托管短 URL」；否则把本地文件转成纯 base64 内容（不带 data: 前缀），以内容字段发出，
    # 避免被中继站当成「素材 URL」做 <1024 字符检查
    image_url = None
    ref_image_b64 = None
    if ref_image_url and str(ref_image_url).strip():
        image_url = str(ref_image_url).strip()
    elif ref_image_path and os.path.exists(ref_image_path):
        try:
            ref_image_b64 = image_to_base64(ref_image_path)
        except Exception:
            ref_image_b64 = None
    if image_url:
        payload["image_url"] = image_url
    elif ref_image_b64:
        payload["image"] = ref_image_b64
        payload["image_base64"] = ref_image_b64

    # 风格参考图：同理
    style_url = None
    style_b64 = None
    if style_ref_url and str(style_ref_url).strip():
        style_url = str(style_ref_url).strip()
    elif style_ref_image_path and os.path.exists(style_ref_image_path):
        try:
            style_b64 = image_to_base64(style_ref_image_path)
        except Exception:
            style_b64 = None
    if style_url:
        payload["reference_image_url"] = style_url
        payload["style_image_url"] = style_url
    elif style_b64:
        payload["reference_image"] = style_b64
        payload["style_image"] = style_b64

    # 视频参考：优先用户托管后的「视频网络地址」；否则把本地 mp4 文件转纯 base64 内容直接发出（JSON 模式，不用 multipart）
    video_local = ref_video_file or ref_video_path
    if not video_local or not os.path.exists(video_local):
        video_local = None
    video_url = (ref_video_url or "").strip()
    if video_url:
        payload["video_url"] = video_url
        payload["reference_video"] = video_url
        payload["input_video"] = video_url
        payload["motion_video"] = video_url
    elif video_local:
        try:
            vb64 = image_to_base64(video_local)
            payload["video"] = vb64
            payload["input_video"] = vb64
            payload["reference_video"] = vb64
            payload["motion_video"] = vb64
        except Exception:
            pass

    # 首尾帧：把第一帧/最后一帧作为首帧/尾帧参考（纯 base64 内容，避免素材 URL 1024 字符限制）
    # 首帧同时作为图生视频起点(image)，并在 first_frame* 字段给出；尾帧只在 last_frame* 字段给出
    if first_frame_path and os.path.exists(first_frame_path):
        try:
            fb64 = image_to_base64(first_frame_path)
            payload["image"] = fb64
            payload["image_base64"] = fb64
            payload["first_frame"] = fb64
            payload["first_frame_image"] = fb64
            payload["start_image"] = fb64
            payload["first_image"] = fb64
        except Exception:
            pass
    if last_frame_path and os.path.exists(last_frame_path):
        try:
            lb64 = image_to_base64(last_frame_path)
            payload["last_frame"] = lb64
            payload["last_frame_image"] = lb64
            payload["end_image"] = lb64
            payload["last_image"] = lb64
        except Exception:
            pass

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_token.strip():
        headers["Authorization"] = f"Bearer {api_token.strip()}"

    try:
        resp = requests.post(api_url, json=payload, headers=headers, timeout=600)
    except requests.exceptions.MissingSchema:
        return {"_error": "视频 API 地址格式错误，需要 http:// 或 https:// 开头"}
    except requests.exceptions.ConnectionError:
        return {"_error": "无法连接视频 API 地址，请检查网站与网络"}
    except requests.exceptions.Timeout:
        return {"_error": "视频生成超时（>600s），模型可能较慢，请重试或缩短时长/降低帧率"}
    is_html, err = _detect_html(resp)
    if is_html:
        return {"_error": err}
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        r = e.response
        et = r.text[:300] if r is not None else str(e)
        if r is not None and r.status_code in (401, 403):
            return {"_error": f"视频 API 鉴权失败 (HTTP {r.status_code})，请检查 Token"}
        return {"_error": f"视频 API HTTP {r.status_code if r is not None else '?'}: {et}"}
    try:
        result = resp.json()
    except ValueError:
        return {"_error": f"视频 API 返回非 JSON 响应: {resp.text[:200]}"}

    # 初始响应就直接带 error（未进入异步轮询就已失败）：立即报错，避免后续误判/傻等
    if isinstance(result, dict) and "error" in result:
        err_obj = result["error"]
        if isinstance(err_obj, dict):
            err_msg = err_obj.get("message") or err_obj.get("msg") or err_obj.get("code") or str(err_obj)
        else:
            err_msg = str(err_obj)
        return {"_error": f"视频生成请求被拒绝: {err_msg}"}

    # fal-ai 异步队列：仅返回 request_id，需要轮询状态
    has_video = any(k in result for k in ("video", "output", "url", "data"))
    if "request_id" in result and not has_video:
        base = api_url.split('/v1/')[0] if '/v1/' in api_url else api_url.rsplit('/', 1)[0]
        status_url = f"{base}/requests/{result['request_id']}"
        auth_header = {"Authorization": headers.get("Authorization", "")} if headers.get("Authorization") else {}
        last_info = ""
        for _ in range(120):  # 120 * 5s = 10 分钟
            time.sleep(5)
            try:
                s = requests.get(status_url, headers=auth_header, timeout=60)
            except Exception:
                continue
            if s.status_code != 200:
                last_info = f"[{s.status_code}] {status_url}"
                continue
            try:
                sj = s.json()
            except ValueError:
                continue
            last_info = f"[{s.status_code}] {status_url}\n{json.dumps(sj, ensure_ascii=False)[:400]}"
            kind, info = _classify_task_status(sj)
            if kind == "success":
                result = sj.get("response", sj) if "response" in sj else sj
                break
            elif kind == "failed":
                return {"_error": f"视频任务失败: {info}"}
            elif kind == "unknown":
                return {"_error": f"视频任务异常结束（{info}）。最后一次轮询：\n{last_info[:300]}"}
            # kind == "progress" → 继续等待
        else:
            return {"_error": f"视频生成轮询超时（>10分钟）。最后一次轮询：\n{last_info}"}

    # 通用异步任务格式（task_id + status: pending/processing/success/failed）
    # 常见于各类中转站，返回 {"status":"pending","task_id":"xxx"} 或类似
    raw_status = result.get("status") or result.get("task_status") or result.get("state") or ""
    task_status = str(raw_status).lower().strip()
    task_id = (result.get("task_id") or result.get("id")
               or result.get("taskId") or result.get("request_id")
               or result.get("batch_id") or result.get("job_id") or result.get("generation_id"))
    # 兼容响应嵌套在 data 里的情况
    if not task_id and isinstance(result.get("data"), dict):
        d = result["data"]
        task_id = (d.get("task_id") or d.get("id") or d.get("request_id") or d.get("generation_id"))
        if not task_status:
            task_status = str(d.get("status") or d.get("task_status") or d.get("state") or "").lower().strip()
    _ASYNC_KW = ("pending", "processing", "queued", "running", "in_progress",
                 "waiting", "scheduled", "submitted", "preparing")
    if ((task_status in _ASYNC_KW) or (task_id and task_status)) and task_id:
        # 构造基础域名（去掉 /v1/... 之后的路径部分）
        base_host = api_url.split('/v1/')[0] if '/v1/' in api_url else api_url.rsplit('/', 1)[0]
        # 优先：POST 响应里直接给出的状态/结果 URL（不同中继字段名各异）
        resp_url_candidates = []
        for k, v in result.items():
            if isinstance(v, str) and v.strip().lower().startswith(("http://", "https://")):
                lv = v.lower()
                if any(t in lv for t in ("task", "status", "generations", "requests",
                                         "result", "query", "fetch", "poll", "job")):
                    resp_url_candidates.append(v.strip())
        # 多种常见轮询端点模式（按最可能排序）
        possible_poll_urls = []
        possible_poll_urls += resp_url_candidates
        possible_poll_urls += [
            f"{api_url}/{task_id}",                                  # .../v1/videos/generations/{id}
            f"{base_host}/v1/videos/generations/{task_id}",           # .../v1/videos/generations/{id}
            f"{base_host}/v1/videos/tasks/{task_id}",                 # .../v1/videos/tasks/{id}
            f"{base_host}/v1/video/tasks/{task_id}",                  # .../v1/video/tasks/{id}
            f"{base_host}/tasks/{task_id}",                          # .../tasks/{id}
            f"{base_host}/v1/tasks/{task_id}",                       # .../v1/tasks/{id}
            f"{base_host}/v1/video/generations/{task_id}",           # .../v1/video/generations/{id}
            f"{base_host}/v1/videos/status/{task_id}",               # .../v1/videos/status/{id}
            f"{api_url}?task_id={task_id}",                          # .../v1/videos/generations?task_id={id}
        ]
        # 去重，保持顺序
        _seen, _deduped = set(), []
        for u in possible_poll_urls:
            if u not in _seen:
                _seen.add(u)
                _deduped.append(u)
        possible_poll_urls = _deduped
        poll_headers = {}
        if headers.get("Authorization"):
            poll_headers["Authorization"] = headers["Authorization"]
        poll_headers["Accept"] = "application/json"
        last_poll_info = ""
        per_url_last = {}
        got_result = None
        no_200_rounds = 0
        for _ in range(120):  # 120 * 5s = 10 分钟
            time.sleep(5)
            round_had_200 = False
            for poll_url in possible_poll_urls:
                try:
                    s = requests.get(poll_url, headers=poll_headers, timeout=30)
                    per_url_last[poll_url] = f"HTTP {s.status_code}"
                    if s.status_code != 200:
                        continue
                    try:
                        sj = s.json()
                    except ValueError:
                        per_url_last[poll_url] = f"HTTP {s.status_code} (非JSON: {s.text[:80]})"
                        continue
                except Exception as e:
                    per_url_last[poll_url] = f"异常: {str(e)[:80]}"
                    continue
                round_had_200 = True
                last_poll_info = f"[{s.status_code}] {poll_url}\n{json.dumps(sj, ensure_ascii=False)[:400]}"
                kind, info = _classify_task_status(sj)
                if kind == "success":
                    got_result = sj
                    break  # 跳出内层循环
                elif kind == "failed":
                    return {"_error": f"视频任务失败: {info}"}
                elif kind == "unknown":
                    return {"_error": f"视频任务异常结束（{info}）。最后一次轮询：\n{last_poll_info[:300]}"}
                # kind == "progress" → 跳出内层循环，继续下一轮等待
            if got_result is not None:
                break  # 跳出外层循环
            if round_had_200:
                no_200_rounds = 0
            else:
                no_200_rounds += 1
                # 连续约 60s 所有轮询地址都非 200（很可能端点路径不对），提前报错避免傻等
                if no_200_rounds >= 12:
                    _diag = "\n".join(f"  {per_url_last.get(u, '未尝试')}  {u}" for u in possible_poll_urls)
                    try:
                        import tempfile, datetime
                        _dbg = os.path.join(tempfile.gettempdir(),
                                            f"ai_video_poll_debug_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt")
                        with open(_dbg, "w", encoding="utf-8") as _f:
                            _f.write("POST 返回 (status/task_id 已识别):\n")
                            _f.write(f"task_id = {task_id}\n")
                            _f.write(json.dumps(result, ensure_ascii=False, indent=2)[:2000] + "\n\n")
                            _f.write("各轮询地址最后一次返回:\n")
                            _f.write(_diag + "\n")
                        _dbg_msg = f"\n详细诊断已写入: {_dbg}"
                    except Exception:
                        _dbg_msg = ""
                    return {"_error": f"视频任务轮询失败：所有轮询地址均返回非 200（可能端点路径不正确）。"
                                     f"\nPOST 返回 task_id={task_id}\n各地址返回:\n{_diag}{_dbg_msg}"}
        if got_result is not None:
            result = got_result
        else:
            _diag = "\n".join(f"  {per_url_last.get(u, '未尝试')}  {u}" for u in possible_poll_urls)
            return {"_error": f"视频生成轮询超时（>10分钟）。各地址返回:\n{_diag}"}

    return result


def _parse_video_response(result):
    """统一解析多种视频返回格式，下载/解码为本地 mp4 文件"""
    if isinstance(result, dict) and "_error" in result:
        return None, result["_error"]

    candidates = []
    # 顶层 video
    v = result.get("video")
    if isinstance(v, str):
        candidates.append(v)
    elif isinstance(v, dict) and isinstance(v.get("url"), str):
        candidates.append(v["url"])
    # 顶层 video_url（常见于中转站）
    if isinstance(result.get("video_url"), str):
        candidates.append(result["video_url"])
    # output.video / output.url / output.video_url
    out = result.get("output")
    if isinstance(out, dict):
        for key in ("video", "video_url", "url"):
            ov = out.get(key)
            if isinstance(ov, str):
                candidates.append(ov)
            elif isinstance(ov, dict) and isinstance(ov.get("url"), str):
                candidates.append(ov["url"])
    # data 包装层
    data = result.get("data")
    if isinstance(data, list) and data:
        item = data[0]
        if isinstance(item, dict):
            for key in ("url", "video_url", "video"):
                val = item.get(key)
                if isinstance(val, str):
                    candidates.append(val)
                elif isinstance(val, dict) and isinstance(val.get("url"), str):
                    candidates.append(val["url"])
            if isinstance(item.get("b64_json"), str):
                candidates.append(item["b64_json"])
    elif isinstance(data, dict):
        # data 可能是直接对象而非列表
        for key in ("url", "video_url", "video"):
            val = data.get(key)
            if isinstance(val, str):
                candidates.append(val)
    # 顶层 url
    if isinstance(result.get("url"), str):
        candidates.append(result["url"])

    for c in candidates:
        if c.startswith("data:video"):
            try:
                b64 = c.split(",", 1)[1]
                raw = base64.b64decode(b64)
                tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
                tmp.write(raw)
                tmp.close()
                return tmp.name, None
            except Exception:
                continue
        elif c.startswith("http"):
            path, err = _download_to_temp(c, suffix=".mp4")
            if path:
                return path, None
            return None, err
    return None, f"视频 API 返回格式异常，响应片段: {str(result)[:300]}"


def _resp_contains_video_url(sj):
    """判断响应中是否含有视频 URL（含常见嵌套结构），用于放宽异步完成判定"""
    if not isinstance(sj, dict):
        return False
    def _is_url(v):
        return isinstance(v, str) and ("http" in v or v.startswith("data:"))
    for k in ("video", "video_url", "url"):
        if _is_url(sj.get(k)):
            return True
    out = sj.get("output")
    if isinstance(out, dict):
        for k in ("video", "video_url", "url"):
            if _is_url(out.get(k)):
                return True
    data = sj.get("data")
    if isinstance(data, dict):
        for k in ("video", "video_url", "url"):
            if _is_url(data.get(k)):
                return True
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        for k in ("video", "video_url", "url"):
            if _is_url(data[0].get(k)):
                return True
    return False


# 异步视频任务状态词集合（统一小写比对）
_VIDEO_TASK_IN_PROGRESS = {
    "pending", "processing", "queued", "queuing", "running",
    "in_progress", "in_queue", "inqueue", "waiting", "wait",
    "scheduled", "submitted", "preparing", "starting", "init",
}
_VIDEO_TASK_SUCCESS = {
    "succeeded", "success", "completed", "complete", "done", "finished",
}
_VIDEO_TASK_FAILED = {
    "failed", "failure", "fail", "errored", "error", "cancelled",
    "canceled", "cancel", "refunded", "refund", "rejected", "reject",
    "expired", "timeout", "timed_out", "timedout", "aborted", "abort",
    "invalid", "denied",
}


def _classify_task_status(sj):
    """把一次轮询响应分类：返回 ('success'|'failed'|'progress'|'unknown', 信息)

    - success: 状态命中成功词，或响应中已含视频 URL（放宽）
    - failed : 状态命中失败词，或响应含 error 字段
    - progress: 状态命中进行中词（继续等待）
    - unknown: 状态非空但既不是成功也不是进行中（视为异常结束，立即报错）
    """
    if not isinstance(sj, dict):
        return "unknown", "轮询响应不是 JSON 对象"
    st = str(sj.get("status", "")).lower().strip()
    has_video = _resp_contains_video_url(sj)
    if st in _VIDEO_TASK_SUCCESS or has_video:
        return "success", ""
    if st in _VIDEO_TASK_FAILED or ("error" in sj and not has_video):
        err_obj = sj.get("error", "")
        if isinstance(err_obj, dict):
            err_msg = err_obj.get("message") or err_obj.get("msg") or str(err_obj)
        else:
            err_msg = str(err_obj)
        return "failed", err_msg or st
    if st:
        if st in _VIDEO_TASK_IN_PROGRESS:
            return "progress", ""
        # 状态非空但既不是成功也不是进行中 → 视为异常结束
        return "unknown", f"status={st}"
    # 状态为空：若含 error 则失败，否则视为进行中继续等
    if "error" in sj:
        err_obj = sj.get("error", "")
        err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
        return "failed", err_msg or "未知错误"
    return "progress", ""


def save_generated_video(video_path, prefix="ai_video", save_dir=None):
    """把生成的视频保存到指定目录。

    保存位置优先级：
      1. save_dir（用户手动指定的目录，非空时优先）
      2. 当前 Blender 文件所在文件夹（bpy.data.filepath 的目录，文件已保存时）
      3. 退回 ~/AI_Generated_Videos/
    """
    try:
        if not save_dir or not save_dir.strip():
            # 尝试用当前 Blender 文件所在文件夹
            if bpy.data.filepath:
                save_dir = os.path.dirname(bpy.data.filepath)
            else:
                save_dir = os.path.join(os.path.expanduser("~"), "AI_Generated_Videos")
        save_dir = save_dir.strip()
        os.makedirs(save_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(save_dir, f"{prefix}_{timestamp}.mp4")
        with open(video_path, "rb") as fs, open(dst, "wb") as fd:
            fd.write(fs.read())
        return dst
    except Exception as e:
        print(f"[AI_Video] 保存失败: {e}")
        return None


def show_video_result_popup(context, success, message):
    """视频生成结束（成功/失败）后弹出明确提示，保证用户可见。

    若当前上下文不允许弹窗（如 modal 定时器里），静默忽略，
    退回 self.report 的提示（调用方仍会 report）。"""
    try:
        def _draw(self, context):
            self.layout.label(text=message)
        context.window_manager.popup_menu(
            _draw,
            title="视频生成" + ("成功" if success else "失败"),
            icon='CHECKMARK' if success else 'ERROR',
        )
    except Exception:
        pass


# =============================================================================
# 四、异步执行 Worker
# =============================================================================

class GenerationWorker(threading.Thread):
    """后台线程：单次 API 调用，把生成的图传回主线程"""
    def __init__(self, props, ref_image_path, result_queue, retry_count=2):
        super().__init__(daemon=True)
        self.props_snapshot = self._snapshot_props(props)
        self.ref_image_path = ref_image_path
        self.result_queue = result_queue
        self.retry_count = retry_count
        self.stop_flag = threading.Event()

    @staticmethod
    def _snapshot_props(props):
        selected_model_id = (props.selected_image_model_id or "").strip()
        return {
            "platform": props.image_platform,
            "platform_url": props.image_platform_url,
            "api_url": props.image_api_url,
            "api_token": props.api_token,
            "comfyui_workflow_file": props.image_comfyui_workflow_file,
            "selected_model_id": selected_model_id,
            "image_size": props.computed_size or props.image_size,
            "denoising_strength": props.denoising_strength,
            "optimize_ref": props.optimize_ref_image,
            "ref_max_size": props.ref_image_max_size,
            "ref_quality": props.ref_image_quality,
            "prompt_content": props.prompt_content,
            "prompt_color": props.prompt_color,
            "prompt_reference": props.prompt_reference,
            "prompt_other": props.prompt_other,
            "style_ref_image": props.style_ref_image,
            "use_scene_object_names": props.use_scene_object_names,
            "scene_object_names": props.scene_object_names,
        }

    def _build_prompt(self):
        """构造 prompt：场景物体名(命名驱动) + 内容 + 色彩 + 参考 + 其他"""
        p = self.props_snapshot
        parts = []
        # 命名驱动：把场景里网格物体的名字作为生成主体，保留整场景镜头构图
        names = (p.get("scene_object_names") or "").strip()
        if p.get("use_scene_object_names", True) and names:
            parts.append(
                f"场景中包含这些命名的物体: {names}。"
                f"保持参考图的镜头构图、空间布局与物体位置完全不变，"
                f"把画面里的每个物体渲染成其名称对应的真实物品，不要新增或删除物体"
            )
        for key in ("prompt_content", "prompt_color", "prompt_reference", "prompt_other"):
            val = (p.get(key) or "").strip()
            if val:
                parts.append(val)
        style_ref = (p.get("style_ref_image") or "").strip()
        if style_ref and os.path.exists(style_ref):
            parts.append(
                "额外提供了一张风格参考图，请让最终画面的整体画风、色调、笔触与质感"
                "尽量贴近这张参考图的风格，同时保持前述的镜头构图与主体内容"
            )
        return ", ".join(parts) if parts else "a rendered scene"

    def run(self):
        p = self.props_snapshot
        prompt = self._build_prompt()

        # 优化参考图：缩放 + 转 JPEG，降低上下文/带宽消耗
        ref_path = self.ref_image_path
        if p.get("optimize_ref", True):
            ref_path = prepare_reference_image(
                self.ref_image_path,
                p.get("ref_max_size", 1024),
                p.get("ref_quality", 85),
            )
            if ref_path != self.ref_image_path and os.path.exists(ref_path):
                orig_kb = os.path.getsize(self.ref_image_path) // 1024
                new_kb = os.path.getsize(ref_path) // 1024
                self.result_queue.put({
                    "type": "progress",
                    "msg": f"已压缩参考图 {orig_kb}KB → {new_kb}KB",
                })

        self.result_queue.put({"type": "progress", "msg": f"调用 API 中: {prompt[:60]}..."})

        img_path, err = call_api(
            platform=p["platform"],
            api_url=p["api_url"],
            api_token=p["api_token"],
            ref_image_path=ref_path,
            prompt=prompt,
            negative_prompt="low quality, blurry, distorted, ugly, watermark",
            retry=self.retry_count,
            model_id=p.get("selected_model_id", ""),
            image_size=p.get("image_size", "1024"),
            denoising=p.get("denoising_strength", 0.5),
            style_ref_image_path=(p.get("style_ref_image") or "").strip() or None,
            platform_url=p.get("platform_url", ""),
            workflow_file=p.get("comfyui_workflow_file", ""),
        )

        if err:
            self.result_queue.put({"type": "error", "error": err})
        else:
            self.result_queue.put({"type": "success", "img_path": img_path})

        self.result_queue.put({"type": "finished"})


class VideoGenerationWorker(threading.Thread):
    """后台线程：把参考图 + 提示词发给视频生成模型，把生成的视频传回主线程"""
    def __init__(self, props, ref_image_path, result_queue, aspect_ratio="16:9",
                 image_size="1280x720", retry_count=1, ref_video_path=None,
                 first_frame_path=None, last_frame_path=None):
        super().__init__(daemon=True)
        self.props_snapshot = self._snapshot_props(props)
        self.ref_image_path = ref_image_path
        self.ref_video_path = ref_video_path
        self.first_frame_path = first_frame_path
        self.last_frame_path = last_frame_path
        self.result_queue = result_queue
        self.aspect_ratio = aspect_ratio
        self.image_size = image_size
        self.retry_count = retry_count
        self.stop_flag = threading.Event()

    @staticmethod
    def _snapshot_props(props):
        selected_model_id = (props.selected_video_model_id or "").strip()
        return {
            "video_api_url": props.video_api_url,
            "api_token": props.api_token,
            "selected_model_id": selected_model_id,
            "video_duration": props.video_duration,
            "optimize_ref": props.optimize_ref_image,
            "ref_max_size": props.ref_image_max_size,
            "ref_quality": props.ref_image_quality,
            "prompt_content": props.prompt_content,
            "prompt_color": props.prompt_color,
            "prompt_reference": props.prompt_reference,
            "prompt_other": props.prompt_other,
            "video_prompt": props.video_prompt,
            "video_gen_mode": props.video_gen_mode,
            "style_ref_image": props.style_ref_image,
            "use_ref_image": props.use_ref_image,
            "ref_image_url": props.ref_image_url,
            "use_style_ref": props.use_style_ref,
            "style_ref_url": props.style_ref_url,
            "ref_video_file": props.ref_video_file,
            "ref_video_url": props.ref_video_url,
            "use_scene_object_names": props.use_scene_object_names,
            "scene_object_names": props.scene_object_names,
            "platform": props.video_platform,
            "platform_url": props.video_platform_url,
            "comfyui_workflow_file": props.video_comfyui_workflow_file,
        }

    def _build_prompt(self):
        """构造 prompt：视频专属提示词 + 场景物体名 + 内容/色彩/参考/其他 + 镜头动画引导"""
        p = self.props_snapshot
        parts = []
        # 视频专属提示词优先（用户主动写，最贴合本次生成意图）
        vp = (p.get("video_prompt") or "").strip()
        if vp:
            parts.append(vp)
        names = (p.get("scene_object_names") or "").strip()
        if p.get("use_scene_object_names", True) and names:
            parts.append(
                f"场景中包含这些命名的物体: {names}。"
                f"保持参考图镜头构图与物体位置不变，让画面中的物体变成其名称对应的真实物品，"
                f"并让它们自然地动起来（轻微运镜/物体运动），不要新增或删除物体"
            )
        for key in ("prompt_content", "prompt_color", "prompt_reference", "prompt_other"):
            val = (p.get(key) or "").strip()
            if val:
                parts.append(val)
        style_ref = (p.get("style_ref_image") or "").strip()
        if style_ref and os.path.exists(style_ref):
            parts.append(
                "额外提供了一张风格参考图，请让最终视频的整体画风、色调与质感"
                "尽量贴近这张参考图的风格，同时保持前述的镜头构图与主体内容"
            )
        # 镜头动画作为运动参考
        if p.get("video_gen_mode") == "motion":
            parts.append(
                "已提供一段参考视频作为镜头运动参考：请严格跟随参考视频中的相机运动轨迹与节奏"
                "（推进/拉远/平移/环绕/俯仰等），让生成的视频镜头运动与参考视频保持一致，"
                "同时保持场景内容不变"
            )
        # 首尾帧生成
        elif p.get("video_gen_mode") == "first_last":
            parts.append(
                "已提供首帧与尾帧两张图片（分别取自相机动画的第一帧与最后一帧）："
                "请生成一段从首帧内容平滑过渡到尾帧内容的视频，保持主体、构图与场景一致，"
                "中间加入自然连贯的运动，不要偏离首尾帧的画面内容"
            )
        # 视频生成额外提示：强调动态、连贯、电影感
        parts.append("smooth cinematic motion, coherent video, no flickering")
        return ", ".join(parts) if parts else "a rendered scene, smooth cinematic motion"

    def run(self):
        try:
            p = self.props_snapshot
            prompt = self._build_prompt()

            # 优化参考图（视频模型同样吃参考图）；纯文生视频时 ref_path 为 None
            ref_path = self.ref_image_path
            if ref_path and p.get("optimize_ref", True):
                ref_path = prepare_reference_image(
                    self.ref_image_path,
                    p.get("ref_max_size", 1024),
                    p.get("ref_quality", 85),
                )
                if ref_path != self.ref_image_path and os.path.exists(ref_path):
                    orig_kb = os.path.getsize(self.ref_image_path) // 1024
                    new_kb = os.path.getsize(ref_path) // 1024
                    self.result_queue.put({
                        "type": "progress",
                        "msg": f"已压缩参考图 {orig_kb}KB → {new_kb}KB",
                    })

            self.result_queue.put({"type": "progress", "msg": f"调用视频 API 中: {prompt[:60]}..."})

            mode = p.get("video_gen_mode", "motion")
            # 首尾帧模式下，主参考图/视频参考由首尾帧取代，清空这些字段避免冲突
            eff_ref_image_url = "" if mode == "first_last" else (p.get("ref_image_url", "") or "")
            eff_ref_video_file = "" if mode == "first_last" else (p.get("ref_video_file", "") or "")
            eff_ref_video_url = "" if mode == "first_last" else (p.get("ref_video_url", "") or "")

            # ComfyUI 视频：走本地 ComfyUI 工作流，不走云端 API
            if p.get("platform") == "comfyui":
                w, h = 1280, 720
                s = str(self.image_size)
                if "x" in s:
                    try:
                        w, h = int(s.split("x")[0]), int(s.split("x")[1])
                    except Exception:
                        pass
                elif s.isdigit():
                    w = h = int(s)
                result = _call_comfyui_video(
                    platform_url=p.get("platform_url", ""),
                    workflow_file=p.get("comfyui_workflow_file", ""),
                    prompt=prompt,
                    ref_video_path=self.ref_video_path,
                    first_frame_path=self.first_frame_path,
                    last_frame_path=self.last_frame_path,
                    ref_image_path=ref_path,
                    model_id=p.get("selected_model_id", ""),
                    width=w,
                    height=h,
                    duration=p.get("video_duration", 4.0),
                )
                if isinstance(result, dict) and "_error" in result:
                    self.result_queue.put({"type": "error", "error": result["_error"]})
                    self.result_queue.put({"type": "finished"})
                    return
                local = (result or {}).get("_local_path")
                if local and os.path.exists(local):
                    self.result_queue.put({"type": "success", "vid_path": local})
                    self.result_queue.put({"type": "finished"})
                    return
                self.result_queue.put({"type": "error", "error": "ComfyUI 未返回视频文件（请确认工作流输出节点是视频，而非图片）"})
                self.result_queue.put({"type": "finished"})
                return

            last_err = None
            for attempt in range(self.retry_count + 1):
                result = _call_video_api(
                    api_url=p["video_api_url"],
                    api_token=p["api_token"],
                    ref_image_path=ref_path,
                    prompt=prompt,
                    duration=p.get("video_duration", 4.0),
                    aspect_ratio=self.aspect_ratio,
                    image_size=self.image_size,
                    style_ref_image_path=((p.get("style_ref_image") or "").strip() if p.get("use_style_ref", True) else "") or None,
                    ref_video_path=self.ref_video_path,
                    model_id=p.get("selected_model_id", ""),
                    ref_image_url=eff_ref_image_url,
                    ref_video_file=eff_ref_video_file,
                    ref_video_url=eff_ref_video_url,
                    style_ref_url=p.get("style_ref_url", ""),
                    first_frame_path=self.first_frame_path,
                    last_frame_path=self.last_frame_path,
                )
                video_path, err = _parse_video_response(result)
                if video_path:
                    self.result_queue.put({"type": "success", "vid_path": video_path})
                    self.result_queue.put({"type": "finished"})
                    return
                last_err = err
                if attempt < self.retry_count:
                    self.result_queue.put({"type": "progress", "msg": f"重试视频生成 ({attempt+1})..."})
                    time.sleep(2)

            self.result_queue.put({"type": "error", "error": last_err or "视频生成失败"})
            self.result_queue.put({"type": "finished"})
        except Exception as e:
            # 任何未捕获异常都必须让主线程知道任务结束，避免 Blender 卡在生成状态
            try:
                import traceback
                tb = traceback.format_exc()
                self.result_queue.put({"type": "error", "error": f"视频生成线程异常: {e}\n{tb[-500:]}"})
            except Exception:
                pass
            try:
                self.result_queue.put({"type": "finished"})
            except Exception:
                pass


# =============================================================================
# 五、属性组
# =============================================================================

class AI_Generate_FailItem(PropertyGroup):
    """单个失败记录"""
    obj_name: StringProperty(name="物体名")
    error: StringProperty(name="错误原因")


class AI_Generate_ModelItem(PropertyGroup):
    """可用模型列表项"""
    model_id: StringProperty(name="模型ID")
    owned_by: StringProperty(name="提供商")
    model_type: StringProperty(name="类型")


class AI_Generate_VideoModelItem(PropertyGroup):
    """可用视频模型列表项"""
    model_id: StringProperty(name="视频模型ID")
    owned_by: StringProperty(name="提供商")
    model_type: StringProperty(name="类型")



class AI_Generate_Properties(PropertyGroup):
    """AI 渲染图/视频生成器 - 所有属性集中管理"""
    # ---------- 平台 / API（已拆分为图片/视频两套独立设置） ----------
    # 旧属性保留，避免已保存的 blend 文件因属性缺失报错；UI 与逻辑已不再使用。
    platform: EnumProperty(
        name="平台(旧)", description="已弃用，仅兼容旧 blend 文件",
        items=PLATFORM_CHOICES, default="api",
    )
    platform_url: StringProperty(
        name="网站地址(旧)", description="已弃用，仅兼容旧 blend 文件",
        default=API_DEFAULT_URL,
    )
    comfyui_workflow_file: StringProperty(
        name="ComfyUI 工作流文件(旧)", description="已弃用，仅兼容旧 blend 文件",
        default="", subtype='FILE_PATH',
    )
    api_url: StringProperty(
        name="API URL(旧)", description="已弃用，仅兼容旧 blend 文件",
        default="",
    )

    # 图片生成独立设置
    image_platform: EnumProperty(
        name="图片平台", description="图片生成的 API 平台类型",
        items=PLATFORM_CHOICES, default="api",
    )
    image_platform_url: StringProperty(
        name="图片网站地址", description="图片生成 API 转发站根地址（不含路径）",
        default=API_DEFAULT_URL,
    )
    image_comfyui_workflow_file: StringProperty(
        name="图片 ComfyUI 工作流文件", description="图片生成用的 ComfyUI API 格式工作流 JSON",
        default="", subtype='FILE_PATH',
    )
    image_api_url: StringProperty(
        name="图片 API URL", description="图片生成的完整接口地址（自动填充，可手动改）",
        default="",
    )

    api_token: StringProperty(
        name="API Token", description="API 密钥 / Bearer Token（图片/视频共享同一 Token）",
        default="", subtype='PASSWORD',
    )

    # ---------- 图像模型选择（原生下拉框） ----------
    selected_image_model_id: EnumProperty(
        name="图像模型", description="当前选中的图像生成模型",
        items=_get_image_model_enum_items,
        update=_on_select_image_model,
    )
    available_models: CollectionProperty(
        name="可用模型列表", type=AI_Generate_ModelItem,
    )
    model_filter: EnumProperty(
        name="过滤", description="模型列表过滤模式",
        items=[
            ("all_image", "仅图像", "只显示图像生成模型", 0),
            ("gpt_google", "GPT/Google", "只显示 OpenAI 和 Google 的图像模型", 1),
            ("all", "全部", "显示所有模型（含非图像）", 2),
            ("video", "仅视频", "只显示视频生成模型", 3),
        ],
        default="all_image",
    )

    # ---------- 视频模型选择（原生下拉框） ----------
    selected_video_model_id: EnumProperty(
        name="视频模型", description="当前选中的视频生成模型",
        items=_get_video_model_enum_items,
        update=_on_select_video_model,
    )
    available_video_models: CollectionProperty(
        name="可用视频模型列表", type=AI_Generate_VideoModelItem,
    )
    # 视频生成独立设置
    video_platform: EnumProperty(
        name="视频平台", description="视频生成的 API 平台类型",
        items=PLATFORM_CHOICES, default="api",
    )
    video_platform_url: StringProperty(
        name="视频网站地址", description="视频生成 API 转发站根地址（不含路径）",
        default=API_DEFAULT_URL,
    )
    video_comfyui_workflow_file: StringProperty(
        name="视频 ComfyUI 工作流文件", description="视频生成用的 ComfyUI API 格式工作流 JSON",
        default="", subtype='FILE_PATH',
    )
    video_api_url: StringProperty(
        name="视频 API URL", description="视频生成的完整接口地址（自动/手动）",
        default="",
    )
    video_model_id_manual: StringProperty(
        name="手动模型ID", description="手动输入视频模型 ID（不使用下拉框时）",
        default="",
    )
    video_models_fetch_status: StringProperty(
        name="获取状态", description="上次获取视频模型的结果提示",
        default="",
    )

    # ---------- 提示词 ----------
    prompt_content: StringProperty(
        name="内容", description="画面主体内容描述",
        default="",
    )
    prompt_color: StringProperty(
        name="色彩", description="色彩风格描述",
        default="",
    )
    prompt_reference: StringProperty(
        name="参考", description="参考风格/艺术家/作品",
        default="",
    )
    prompt_other: StringProperty(
        name="其他", description="其他补充要求（质量、构图、光影等）",
        default="",
    )
    style_ref_image: StringProperty(
        name="风格参考图", description="上传一张风格参考图，生成时将尽量匹配其风格/画风",
        default="", subtype='FILE_PATH',
    )
    use_scene_object_names: BoolProperty(
        name="用场景物体名作为内容", description="自动捕获场景中物体的名称并写入 prompt",
        default=True,
    )
    scene_object_names: StringProperty(
        name="场景物体名", description="自动捕获的物体名称（内部用）",
        default="",
    )

    # ---------- 输出参数 ----------
    image_size: IntProperty(
        name="分辨率", description="输出图片边长（正方形）或短边（像素）",
        default=1024, min=256, max=4096, soft_max=2048,
    )
    computed_size: StringProperty(
        name="计算尺寸", description="跟随镜头比例时自动计算的尺寸（内部用）",
        default="",
    )
    follow_camera_aspect: BoolProperty(
        name="跟随镜头", description="自动按摄像机画面比例计算输出尺寸",
        default=True,
    )
    denoising_strength: FloatProperty(
        name="重绘强度", description="图生图的重绘强度（0=完全保留原图，1=完全重绘）",
        default=0.5, min=0.0, max=1.0, precision=2,
    )
    auto_save_image: BoolProperty(
        name="自动保存图片", description="生成后自动保存到 ~/AI_Generated_Images/",
        default=True,
    )
    optimize_ref_image: BoolProperty(
        name="优化参考图", description="发送前压缩参考图（缩小+JPEG）以节省带宽和费用",
        default=True,
    )
    ref_image_max_size: IntProperty(
        name="最大边长", description="参考图最大边长（像素）",
        default=1024, min=256, max=4096,
    )
    ref_image_quality: IntProperty(
        name="质量", description="参考图 JPEG 质量（1-100）",
        default=85, min=1, max=100,

    )

    # ---------- 视频参数 ----------
    video_duration: FloatProperty(
        name="时长(秒)", description="生成视频的时长（秒）",
        default=4.0, min=1.0, max=30.0, precision=1,
    )
    use_ref_image: BoolProperty(
        name="使用渲染参考图", description="把 Blender 当前视图渲染为参考图（图生视频）。关闭则为纯文生视频",
        default=True,
    )
    ref_image_url: StringProperty(
        name="参考图网络地址(可选)", description="已托管、可公网访问的短 URL（<1024字符）。填写后优先用它作参考图，"
                                                  "可绕过部分中继站拒绝 base64 data URL(超1024字符)的限制",
        default="",
    )
    use_style_ref: BoolProperty(
        name="使用风格参考图", description="发送风格参考图（本地文件或下方网络地址）",
        default=True,
    )
    style_ref_url: StringProperty(
        name="风格图网络地址(可选)", description="已托管、可公网访问的短 URL（<1024字符），用作风格参考",
        default="",
    )
    auto_save_video: BoolProperty(
        name="自动保存视频", description="生成后自动保存视频文件",
        default=True,
    )
    video_save_dir: StringProperty(
        name="保存位置",
        description="视频保存目录（留空则保存到当前 Blender 文件所在文件夹，若未保存过则退回 ~/AI_Generated_Videos/）",
        default="",
        subtype='DIR_PATH',
    )

    # ---------- 视频提示词 & 镜头参考 ----------
    video_prompt: StringProperty(
        name="视频提示词", description="视频生成的专属提示词（描述运动/镜头/画面变化），与图片提示词相互独立",
        default="",
    )
    video_gen_mode: EnumProperty(
        name="视频参考方式",
        description="选择视频生成的参考方式：镜头动画作为运动参考，或用相机动画的首尾帧生成过渡视频",
        items=[
            ("motion", "镜头动画参考", "渲染 Blender 相机动画为视频，作为镜头运动参考（视频生视频）"),
            ("first_last", "首尾帧", "用相机动画的第一帧和最后一帧作为首帧/尾帧，生成从首帧过渡到尾帧的视频"),
        ],
        default="motion",
    )
    ref_video_file: StringProperty(
        name="参考视频文件(mp4)", description="直接选择本地 mp4/mov/webm 文件作为镜头运动参考（以 base64 形式随 JSON 发出，绕过素材URL 1024字符限制）。留空则使用上面的镜头动画",
        default="",
        subtype='FILE_PATH',
    )
    ref_video_url: StringProperty(
        name="参考视频网络地址(可选)", description="已托管、可公网访问的短 URL（<1024字符）。填写后优先用它作视频参考，最稳定；留空则用上方本地文件转 base64",
        default="",
    )

    # ---------- 运行状态 ----------
    is_generating: BoolProperty(
        name="正在生成图片", description="图片生成线程是否在运行",
        default=False,
    )
    progress_text: StringProperty(
        name="进度文本", description="图片生成进度/状态文字",
        default="",
    )
    last_image_path: StringProperty(
        name="最近图片路径", description="最近一次成功生成的图片文件路径",
        default="",
    )
    is_generating_video: BoolProperty(
        name="正在生成视频", description="视频生成线程是否在运行",
        default=False,
    )
    progress_text_video: StringProperty(
        name="视频进度文本", description="视频生成进度/状态文字",
        default="",
    )
    last_video_path: StringProperty(
        name="最近视频路径", description="最近一次成功生成的视频文件路径",
        default="",
    )

    # ---------- 折叠状态 ----------
    image_section_expanded: BoolProperty(
        name="图片区展开", description="生成图片区块是否展开",
        default=True,
    )
    video_section_expanded: BoolProperty(
        name="视频区展开", description="生成视频区块是否展开",
        default=True,
    )

    # ---------- 兼容属性（原始操作符引用）----------
    selected_model_index: IntProperty(
        name="选中模型索引", description="模型列表中当前选中的索引（内部用）",
        default=0,
    )
    last_error: StringProperty(
        name="最近错误", description="最近一次操作的错误信息",
        default="",
    )
    models_fetch_status: StringProperty(
        name="图像获取状态", description="上次获取图像模型的结果提示",
        default="",
    )

    # ---------- 失败记录 ----------
    fail_details: CollectionProperty(
        name="失败详情", type=AI_Generate_FailItem,
    )
    fail_details_video: CollectionProperty(
        name="视频失败详情", type=AI_Generate_FailItem,
    )



# =============================================================================
# 核心操作符：渲染预览 / 获取模型 / 生成图片 / 查看结果
# =============================================================================

class OBJECT_OT_Render_Ref_Preview(Operator):
    """渲染摄像机视图为参考图"""
    bl_idname = "object.render_ref_preview"
    bl_label = "预览摄像机视图"
    bl_options = {'REGISTER'}

    def execute(self, context):
        path, err = render_camera_view_to_temp(resolution_percentage=50)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        img = bpy.data.images.load(path, check_existing=True)
        for area in context.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                area.spaces.active.image = img
                self.report({'INFO'}, "已渲染并显示在图像编辑器")
                return {'FINISHED'}
        self.report({'INFO'}, "已渲染（请打开图像编辑器查看）")
        return {'FINISHED'}


class OBJECT_OT_AI_Fetch_Models(Operator):
    """获取图像模型列表"""
    bl_idname = "object.ai_fetch_models"
    bl_label = "获取模型列表"
    bl_options = {'REGISTER'}

    def execute(self, context):
        props = context.scene.ai_gen_props
        if not props.image_platform_url.strip():
            self.report({'ERROR'}, "请先填写图片网站地址")
            return {'CANCELLED'}
        if props.image_platform != "comfyui" and not props.api_token.strip():
            self.report({'ERROR'}, "请先填写 API Token")
            return {'CANCELLED'}

        self.report({'INFO'}, "正在从 " + props.image_platform_url + " 获取图片模型列表...")
        props.models_fetch_status = "拉取中..."

        models, err = fetch_models_from_api(props.image_platform_url, props.api_token, platform=props.image_platform)
        if err:
            self.report({'ERROR'}, "获取失败: " + err)
            props.models_fetch_status = "X " + err[:60]
            props.last_error = err
            return {'CANCELLED'}

        total_count = len(models)
        models = filter_models_list(models, props.model_filter)
        filtered_count = len(models)

        if filtered_count == 0:
            props.available_models.clear()
            props.models_fetch_status = "X 网站共 " + str(total_count) + " 个模型，无匹配"
            self.report({'WARNING'}, "网站共 " + str(total_count) + " 个模型，但过滤后无匹配")
            return {'CANCELLED'}

        props.available_models.clear()
        for m in models:
            item = props.available_models.add()
            item.model_id = m["id"]
            item.owned_by = m["owned_by"]
            is_img, _ = is_image_generation_model(m["id"])
            item.model_type = "image" if is_img else "text"

        # 填充全局枚举缓存（供 EnumProperty 下拉框使用）
        global _image_model_enum_items
        _image_model_enum_items.clear()
        for i, m in enumerate(models):
            _image_model_enum_items.append((m["id"], m["id"], f"by {m['owned_by']}", i))

        props.models_fetch_status = "OK 显示 " + str(filtered_count) + "/" + str(total_count) + " 个模型"
        self.report({'INFO'}, "获取到 " + str(total_count) + " 个，过滤后 " + str(filtered_count) + " 个")
        self.report({'INFO'}, "请在上方下拉框自行选择模型")
        return {'FINISHED'}


class OBJECT_OT_AI_Apply_Selected_Model(Operator):
    """应用选中的模型到 API URL"""
    bl_idname = "object.ai_apply_selected_model"
    bl_label = "应用选中模型"

    def execute(self, context):
        props = context.scene.ai_gen_props
        if 0 <= props.selected_model_index < len(props.available_models):
            item = props.available_models[props.selected_model_index]
            props.image_api_url = build_api_url_for_model(props.image_platform_url, item.model_id, platform=props.image_platform)
            props.selected_image_model_id = item.model_id
            if item.model_type != "image":
                self.report({'WARNING'}, item.model_id + " 看起来不是图生图模型")
            else:
                self.report({'INFO'}, "已应用: " + item.model_id)
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "列表中无选中项")
            return {'CANCELLED'}


class OBJECT_OT_AI_Generate_Async(Operator):
    """开始 AI 图片生成：渲染视图 -> API -> 显示结果"""
    bl_idname = "object.ai_generate_async"
    bl_label = "生成图片"
    bl_options = {'REGISTER'}

    _timer = None
    _worker = None
    _queue = None

    def execute(self, context):
        props = context.scene.ai_gen_props
        if props.is_generating:
            self.report({'WARNING'}, "已有生成任务进行中")
            return {'CANCELLED'}
        if props.image_platform == "comfyui":
            if not props.image_comfyui_workflow_file.strip() or not os.path.exists(props.image_comfyui_workflow_file):
                self.report({'ERROR'}, "请先选择图片 ComfyUI 工作流 JSON 文件（右键工作流→保存(API 格式)）")
                return {'CANCELLED'}
        else:
            if not props.image_api_url.strip():
                self.report({'ERROR'}, "图片 API 地址不能为空")
                return {'CANCELLED'}

        # 渲染参考底图
        ref_path, err = render_camera_view_to_temp(resolution_percentage=50)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}

        # 计算输出尺寸
        raw = str(props.image_size).strip()
        try:
            base = int(raw) if raw.isdigit() else 1536
        except Exception:
            base = 1536
        if props.follow_camera_aspect:
            rx = context.scene.render.resolution_x
            ry = context.scene.render.resolution_y
            if rx <= 0 or ry <= 0:
                rx, ry = 16, 9
            aspect = rx / float(ry)
            if aspect >= 1.0:
                w, h = base, max(1, int(round(base / aspect)))
            else:
                h, w = base, max(1, int(round(base * aspect)))
            props.computed_size = str(w) + "x" + str(h)
        else:
            props.computed_size = str(base) + "x" + str(base)

        # 收集场景物体名
        mesh_names = [o.name for o in context.scene.objects if o.type == 'MESH']
        props.scene_object_names = "、".join(mesh_names)

        # 启动异步线程
        self._queue = queue.Queue()
        self._worker = GenerationWorker(props, ref_path, self._queue, retry_count=2)

        props.is_generating = True
        props.progress_text = "调用 API 中..."
        props.last_error = ""
        props.fail_details.clear()
        self._worker.start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        props = context.scene.ai_gen_props
        if event.type == 'TIMER':
            while not self._queue.empty():
                try:
                    msg = self._queue.get_nowait()
                except queue.Empty:
                    break
                mtype = msg.get("type")
                if mtype == "progress":
                    props.progress_text = msg.get("msg", "处理中...")
                    context.area.tag_redraw()
                elif mtype == "success":
                    img_path = msg["img_path"]
                    props.last_image_path = img_path
                    saved_path = None
                    try:
                        img = bpy.data.images.load(img_path, check_existing=True)
                        shown = False
                        for area in context.screen.areas:
                            if area.type == 'IMAGE_EDITOR':
                                area.spaces.active.image = img
                                shown = True
                                break
                        if not shown:
                            # 优先把不重要的面板（控制台/信息/大纲等）切换为图像编辑器，避免破坏 3D 视图
                            for area in context.screen.areas:
                                if area.type in ('CONSOLE', 'INFO', 'OUTLINER', 'PROPERTIES',
                                                'NLA_EDITOR', 'GRAPH_EDITOR', 'DOPESHEET_EDITOR',
                                                'TIMELINE', 'SEQUENCE_EDITOR'):
                                    area.type = 'IMAGE_EDITOR'
                                    area.spaces.active.image = img
                                    shown = True
                                    break
                        if shown:
                            self.report({'INFO'}, "OK 生成成功，已在图像编辑器显示")
                        else:
                            self.report({'WARNING'}, "已生成，但未找到可显示的面板，请打开图像编辑器查看")
                    except Exception as e:
                        self.report({'ERROR'}, "加载图片失败: " + str(e))
                    if props.auto_save_image:
                        saved = save_generated_image(img_path)
                        if saved:
                            saved_path = saved
                            self.report({'INFO'}, "已保存到: " + saved)
                    if saved_path:
                        props.progress_text = "OK 生成完成（已存: " + saved_path + "）"
                    else:
                        props.progress_text = "OK 生成完成（看图像编辑器）"
                    context.area.tag_redraw()
                elif mtype == "error":
                    err = msg.get("error", "未知错误")
                    props.last_error = err
                    props.progress_text = "X 失败: " + err[:60]
                    item = props.fail_details.add()
                    item.obj_name = "(生成)"
                    item.error = err[:500]
                    self.report({'ERROR'}, err)
                    context.area.tag_redraw()
                elif mtype == "finished":
                    props.is_generating = False
                    self._cleanup(context)
                    return {'FINISHED'}
            if not self._worker.is_alive() and self._queue.empty():
                props.is_generating = False
                self._cleanup(context)
                return {'FINISHED'}
        return {'PASS_THROUGH'}

    def _cleanup(self, context):
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        self._worker = None
        self._queue = None

    def cancel(self, context):
        props = context.scene.ai_gen_props
        if self._worker:
            self._worker.stop_flag.set()
        props.is_generating = False
        props.progress_text = "正在取消..."
        self._cleanup(context)


class OBJECT_OT_AI_Show_Last_Image(Operator):
    """重新显示最近一次生成的图片"""
    bl_idname = "object.ai_show_last_image"
    bl_label = "重新显示最近生成图"

    def execute(self, context):
        props = context.scene.ai_gen_props
        if not props.last_image_path or not os.path.exists(props.last_image_path):
            self.report({'WARNING'}, "暂无生成图")
            return {'CANCELLED'}
        try:
            img = bpy.data.images.load(props.last_image_path, check_existing=True)
            for area in context.screen.areas:
                if area.type == 'IMAGE_EDITOR':
                    area.spaces.active.image = img
                    self.report({'INFO'}, "已显示")
                    return {'FINISHED'}
            self.report({'WARNING'}, "未找到图像编辑器")
        except Exception as e:
            self.report({'ERROR'}, "加载失败: " + str(e))
        return {'CANCELLED'}


class OBJECT_OT_AI_Copy_Errors(Operator):
    """复制失败详情到剪贴板"""
    bl_idname = "object.ai_copy_errors"
    bl_label = "复制错误"

    def execute(self, context):
        props = context.scene.ai_gen_props
        if len(props.fail_details) == 0:
            self.report({'WARNING'}, "无失败记录")
            return {'CANCELLED'}
        lines = [
            "AI 渲染图/视频生成器 - 失败详情",
            "时间: " + time.strftime('%Y-%m-%d %H:%M:%S'),
            "图片平台: " + props.image_platform,
            "图片网站: " + props.image_platform_url,
            "图片 API URL: " + props.image_api_url,
            "模型: " + props.selected_image_model_id or "(未选)",
            "",
        ]
        for f in props.fail_details:
            lines.append("X " + f.obj_name)
            lines.append("    " + f.error)
            lines.append("")
        text = "\n".join(lines)
        try:
            context.window_manager.clipboard = text
            self.report({'INFO'}, "已复制 " + str(len(props.fail_details)) + " 条错误到剪贴板")
        except Exception as e:
            self.report({'ERROR'}, "复制失败: " + str(e))
        return {'FINISHED'}


class OBJECT_OT_AI_Clear_Fails(Operator):
    """清空失败记录"""
    bl_idname = "object.ai_clear_fails"
    bl_label = "清空失败记录"

    def execute(self, context):
        props = context.scene.ai_gen_props
        props.fail_details.clear()
        props.last_error = ""
        self.report({'INFO'}, "已清空")
        return {'FINISHED'}


class OBJECT_OT_AI_Load_Style_Ref(Operator):
    """上传风格参考图（文件浏览器）"""
    bl_idname = "object.ai_load_style_ref"
    bl_label = "上传风格参考图"
    bl_options = {'REGISTER'}

    filepath: StringProperty(subtype='FILE_PATH', default="")
    filter_glob: StringProperty(
        default="*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif",
        options={'HIDDEN'},
    )

    def execute(self, context):
        props = context.scene.ai_gen_props
        if not self.filepath or not os.path.exists(self.filepath):
            self.report({'ERROR'}, "文件不存在")
            return {'CANCELLED'}
        props.style_ref_image = self.filepath
        self.report({'INFO'}, "风格参考图 → " + os.path.basename(self.filepath))
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class OBJECT_OT_AI_Clear_Style_Ref(Operator):
    """清除已上传的风格参考图"""
    bl_idname = "object.ai_clear_style_ref"
    bl_label = "清除风格参考图"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        props = context.scene.ai_gen_props
        props.style_ref_image = ""
        self.report({'INFO'}, "已清除风格参考图")
        return {'FINISHED'}


# =============================================================================
# 视频核心操作符：获取视频模型 / 生成视频 / 查看结果
# =============================================================================

class OBJECT_OT_AI_Fetch_Video_Models(Operator):
    """获取视频模型列表"""
    bl_idname = "object.ai_fetch_video_models"
    bl_label = "获取视频模型"
    bl_options = {'REGISTER'}

    def execute(self, context):
        props = context.scene.ai_gen_props
        if not props.video_platform_url.strip():
            self.report({'ERROR'}, "请先填写视频网站地址 / ComfyUI 地址")
            return {'CANCELLED'}
        if props.video_platform == "comfyui":
            # ComfyUI：从本地服务拉取 checkpoint 作为可选视频模型（工作流也可自带模型）
            models, err = fetch_models_from_api(props.video_platform_url, "", platform="comfyui")
        else:
            if not props.api_token.strip():
                self.report({'ERROR'}, "请先填写 API Token")
                return {'CANCELLED'}
            models, err = fetch_models_from_api(props.video_platform_url, props.api_token, platform=props.video_platform)
        if err:
            self.report({'ERROR'}, "获取失败: " + err)
            props.video_models_fetch_status = "X " + err[:60]
            return {'CANCELLED'}

        # 按照过滤下拉框（model_filter）过滤模型，和图片区保持一致
        total_count = len(models)
        models = filter_models_list(models, props.model_filter)
        filtered_count = len(models)

        if filtered_count == 0:
            props.available_video_models.clear()
            _video_model_enum_items.clear()
            props.video_models_fetch_status = "X 共 " + str(total_count) + " 个模型，按过滤后无匹配"
            self.report({'WARNING'}, "共 " + str(total_count) + " 个模型，但按过滤后无匹配")
            return {'CANCELLED'}

        props.available_video_models.clear()
        for m in models:
            item = props.available_video_models.add()
            item.model_id = m["id"]
            item.owned_by = m["owned_by"]
            is_vid, _ = is_video_generation_model(m["id"])
            item.model_type = "video" if is_vid else "image"

        # 填充全局枚举缓存（供 EnumProperty 下拉框使用）
        _video_model_enum_items.clear()
        for i, m in enumerate(models):
            _video_model_enum_items.append((m["id"], m["id"], f"by {m['owned_by']}", i))

        props.video_models_fetch_status = "OK 显示 " + str(filtered_count) + "/" + str(total_count) + " 个模型"
        self.report({'INFO'}, "获取到 " + str(total_count) + " 个，按过滤后 " + str(filtered_count) + " 个")
        self.report({'INFO'}, "请在上方下拉框自行选择模型")
        return {'FINISHED'}


class OBJECT_OT_AI_Apply_Video_Model(Operator):
    """应用选中的视频模型"""
    bl_idname = "object.ai_apply_video_model"
    bl_label = "应用视频模型"

    def execute(self, context):
        props = context.scene.ai_gen_props
        mid = props.selected_video_model_id
        if not mid:
            self.report({'WARNING'}, "未选择视频模型")
            return {'CANCELLED'}
        props.video_api_url = build_video_api_url(props.video_platform_url, mid)
        props.video_model_id_manual = mid
        self.report({'INFO'}, "已应用视频模型: " + mid)
        return {'FINISHED'}


class OBJECT_OT_AI_Generate_Video_Async(Operator):
    """开始 AI 视频生成：渲染视图 -> 视频 API -> 保存本地"""
    bl_idname = "object.ai_generate_video_async"
    bl_label = "生成视频"
    bl_options = {'REGISTER'}

    _timer = None
    _worker = None
    _queue = None

    def execute(self, context):
        props = context.scene.ai_gen_props
        if props.is_generating_video:
            # 安全兜底：如果 worker 线程其实已经死了，自动重置状态（防止卡死）
            if self._worker is not None and not self._worker.is_alive():
                props.is_generating_video = False
                props.progress_text_video = ""
                self._worker = None
                self._queue = None
                self.report({'WARNING'}, "检测到上一次任务已异常退出，已自动重置")
            else:
                self.report({'WARNING'}, "已有视频生成任务进行中，请等待完成或点击取消")
                return {'CANCELLED'}
        if props.video_platform == "comfyui":
            if not props.video_comfyui_workflow_file.strip() or not os.path.exists(props.video_comfyui_workflow_file):
                self.report({'ERROR'}, "请先选择视频 ComfyUI 工作流 JSON 文件")
                return {'CANCELLED'}
        else:
            # 每次生成都用「网站地址 + 选中的模型」重新拼 URL，避免残留的旧地址（如单数 /v1/video/generations）导致 404
            mid = (props.selected_video_model_id or "").strip()
            if mid and props.video_platform_url.strip():
                props.video_api_url = build_video_api_url(props.video_platform_url, mid)
            if not props.video_api_url.strip() and not props.selected_video_model_id:
                self.report({'ERROR'}, "视频 API 地址或模型 ID 不能为空")
                return {'CANCELLED'}

        mode = props.video_gen_mode
        ref_image_url = (props.ref_image_url or "").strip()
        ref_path = None
        cam_video_path = None
        first_frame_path = None
        last_frame_path = None

        if mode == "first_last":
            # 首尾帧：首帧=相机动画第一帧，尾帧=最后一帧；首帧同时作为图生视频起点
            self.report({'INFO'}, "正在渲染相机动画首尾帧...")
            first_frame_path, last_frame_path, fl_err = render_camera_first_last_frames(resolution_percentage=50)
            if fl_err:
                self.report({'ERROR'}, fl_err)
                return {'CANCELLED'}
            self.report({'INFO'}, "首尾帧已渲染，将作为首帧/尾帧参考（首帧兼作起点）")
        else:
            # 镜头动画参考：本地 mp4 优先，否则渲染相机动画为视频
            ref_video_file = (props.ref_video_file or "").strip()
            if ref_video_file and os.path.exists(ref_video_file):
                cam_video_path = ref_video_file
                vsize_kb = os.path.getsize(cam_video_path) // 1024
                self.report({'INFO'}, f"已选择参考视频文件: {vsize_kb}KB，将作为运动参考上传")
            else:
                self.report({'INFO'}, "正在渲染镜头动画作为参考...")
                cam_video_path, anim_err = render_camera_animation_to_temp(resolution_percentage=35)
                if anim_err:
                    self.report({'ERROR'}, anim_err)
                    return {'CANCELLED'}
                vsize_kb = os.path.getsize(cam_video_path) // 1024
                self.report({'INFO'}, f"镜头动画已渲染: {vsize_kb}KB，将作为运动参考上传")
            # 主参考图（图生视频起点）：优先用托管 URL，否则按开关渲染 Blender 首帧
            if not ref_image_url:
                if props.use_ref_image:
                    ref_path, err = render_camera_view_to_temp(resolution_percentage=50)
                    if err:
                        self.report({'ERROR'}, err)
                        return {'CANCELLED'}
                else:
                    self.report({'INFO'}, "未使用参考图，将生成纯文生视频")

        # 启动异步线程：retry_count=0 保证一次点击只提交一次请求，避免服务端重复生成多个视频
        self._queue = queue.Queue()
        self._worker = VideoGenerationWorker(
            props, ref_path, self._queue, retry_count=0, ref_video_path=cam_video_path,
            first_frame_path=first_frame_path, last_frame_path=last_frame_path,
        )

        props.is_generating_video = True
        props.progress_text_video = "调用视频 API 中..."
        props.fail_details_video.clear()
        self._result_ok = None
        self._result_msg = ""
        self._worker.start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.5, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        props = context.scene.ai_gen_props
        if event.type == 'TIMER':
            while not self._queue.empty():
                try:
                    msg = self._queue.get_nowait()
                except queue.Empty:
                    break
                mtype = msg.get("type")
                if mtype == "progress":
                    props.progress_text_video = msg.get("msg", "处理中...")
                    context.area.tag_redraw()
                elif mtype == "success":
                    vid_path = msg["vid_path"]
                    props.last_video_path = vid_path
                    saved_path = ""
                    if props.auto_save_video:
                        saved = save_generated_video(
                            vid_path, save_dir=props.video_save_dir.strip()
                        )
                        if saved:
                            saved_path = saved
                            self.report({'INFO'}, "视频已保存: " + saved)
                    props.progress_text_video = "OK 视频生成完成!"
                    self.report({'INFO'}, "OK 视频生成完成!")
                    self._result_ok = True
                    self._result_msg = ("视频已保存: " + saved_path) if saved_path else "视频生成完成（未自动保存）"
                    context.area.tag_redraw()
                elif mtype == "error":
                    err = msg.get("error", "未知错误")
                    props.progress_text_video = "X 失败: " + err[:60]
                    item = props.fail_details_video.add()
                    item.obj_name = "(视频生成)"
                    item.error = err[:500]
                    self.report({'ERROR'}, err)
                    self._result_ok = False
                    self._result_msg = "生成失败: " + err[:300]
                    context.area.tag_redraw()
                elif mtype == "finished":
                    if self._result_ok is not None:
                        show_video_result_popup(context, self._result_ok, self._result_msg)
                    props.is_generating_video = False
                    self._cleanup(context)
                    return {'FINISHED'}
            if not self._worker.is_alive() and self._queue.empty():
                if self._result_ok is not None:
                    show_video_result_popup(context, self._result_ok, self._result_msg)
                props.is_generating_video = False
                self._cleanup(context)
                return {'FINISHED'}
        return {'PASS_THROUGH'}

    def _cleanup(self, context):
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        self._worker = None
        self._queue = None

    def cancel(self, context):
        props = context.scene.ai_gen_props
        if self._worker:
            self._worker.stop_flag.set()
        props.is_generating_video = False
        props.progress_text_video = "正在取消..."
        self._cleanup(context)


class OBJECT_OT_AI_Cancel_Video(Operator):
    """取消当前视频生成任务 / 重置卡死的状态"""
    bl_idname = "object.ai_cancel_video"
    bl_label = "✕ 取消 / 重置"

    def execute(self, context):
        props = context.scene.ai_gen_props
        # 尝试通知正在运行的生成操作符取消
        gen_op = context.window_manager.operators.get("OBJECT_OT_AI_Generate_Video_Async")
        if gen_op is not None:
            gen_op.cancel(context)
        # 无条件重置状态（防止卡死）
        props.is_generating_video = False
        props.progress_text_video = ""
        self.report({'INFO'}, "已重置视频生成状态，可以重新生成")
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}


class OBJECT_OT_AI_Show_Last_Video(Operator):
    """打开最近生成的视频文件"""
    bl_idname = "object.ai_show_last_video"
    bl_label = "播放最近视频"

    def execute(self, context):
        props = context.scene.ai_gen_props
        if not props.last_video_path or not os.path.exists(props.last_video_path):
            self.report({'WARNING'}, "暂无视频")
            return {'CANCELLED'}
        import subprocess
        import sys
        try:
            if sys.platform == 'win32':
                os.startfile(props.last_video_path)
            elif sys.platform == 'darwin':
                subprocess.call(['open', props.last_video_path])
            else:
                subprocess.call(['xdg-open', props.last_video_path])
            self.report({'INFO'}, "已打开视频")
        except Exception as e:
            self.report({'ERROR'}, "打开失败: " + str(e))
        return {'FINISHED'}


class OBJECT_OT_AI_Open_Video_Folder(Operator):
    """打开视频输出目录"""
    bl_idname = "object.ai_open_video_folder"
    bl_label = "打开视频目录"

    def execute(self, context):
        import subprocess
        video_dir = os.path.join(os.path.expanduser("~"), "AI_Generated_Videos")
        if not os.path.exists(video_dir):
            os.makedirs(video_dir, exist_ok=True)
        try:
            if sys.platform == 'win32':
                subprocess.Popen(['explorer', video_dir])
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', video_dir])
            else:
                subprocess.Popen(['xdg-open', video_dir])
            self.report({'INFO'}, "已打开视频目录")
        except Exception as e:
            self.report({'ERROR'}, "打开失败: " + str(e))
        return {'FINISHED'}


class OBJECT_OT_AI_Copy_Video_Errors(Operator):
    """复制视频失败详情到剪贴板"""
    bl_idname = "object.ai_copy_video_errors"
    bl_label = "复制视频错误"

    def execute(self, context):
        props = context.scene.ai_gen_props
        if len(props.fail_details_video) == 0:
            self.report({'WARNING'}, "无视频失败记录")
            return {'CANCELLED'}
        lines = [
            "AI 视频生成器 - 失败详情",
            "时间: " + time.strftime('%Y-%m-%d %H:%M:%S'),
            "视频平台: " + props.video_platform,
            "视频网站: " + props.video_platform_url,
            "视频 API: " + props.video_api_url,
            "模型: " + props.selected_video_model_id or "(未选)",
            "",
        ]
        for f in props.fail_details_video:
            lines.append("X " + f.obj_name)
            lines.append("    " + f.error)
            lines.append("")
        text = "\n".join(lines)
        try:
            context.window_manager.clipboard = text
            self.report({'INFO'}, "已复制 " + str(len(props.fail_details_video)) + " 条错误")
        except Exception as e:
            self.report({'ERROR'}, "复制失败: " + str(e))
        return {'FINISHED'}


class OBJECT_OT_AI_Clear_Video_Fails(Operator):
    """清空视频失败记录"""
    bl_idname = "object.ai_clear_video_fails"
    bl_label = "清空视频失败记录"

    def execute(self, context):
        props = context.scene.ai_gen_props
        props.fail_details_video.clear()
        self.report({'INFO'}, "已清空")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# 模型下拉框（EnumProperty）辅助函数：图像 / 视频 各一个独立下拉框
# -----------------------------------------------------------------------------

# =============================================================================
# 图像 / 视频模型选择操作符：各自弹出一个独立选择菜单（替代 EnumProperty 下拉框）
# =============================================================================

class OBJECT_OT_AI_Pick_Image_Model(Operator):
    bl_idname = "object.ai_pick_image_model"
    bl_label = "选择图像模型"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        return {'FINISHED'}

    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        props = context.scene.ai_gen_props
        col = layout.column(align=True)
        col.label(text="选择图像生成模型:", icon='OUTLINER_OB_IMAGE')
        col.separator()
        if len(props.available_models) > 0:
            for item in props.available_models:
                is_sel = (props.selected_image_model_id == item.model_id)
                icon = 'IMAGE_DATA' if (item.model_type == "image") else 'TEXT'
                row = col.row()
                if is_sel:
                    row.enabled = False
                    row.label(text="✓ " + item.model_id, icon='CHECKMARK')
                else:
                    op = row.operator("object.ai_set_image_model", text=item.model_id, icon=icon)
                    op.model_id = item.model_id
                if item.owned_by:
                    row.label(text=item.owned_by, icon='DOT')
        else:
            col.label(text="(无可用模型 · 请先点击「获取模型列表」)", icon='ERROR')
        col.separator()

class OBJECT_OT_AI_Set_Image_Model(Operator):
    bl_idname = "object.ai_set_image_model"
    bl_label = "设置图像模型"
    model_id: StringProperty(name="模型ID", default="")

    def execute(self, context):
        props = context.scene.ai_gen_props
        mid = self.model_id.strip()
        if not mid:
            return {'CANCELLED'}
        props.selected_image_model_id = mid
        props.image_api_url = build_api_url_for_model(props.image_platform_url, mid, platform=props.image_platform)
        self.report({'INFO'}, "图像模型 → " + mid)
        return {'FINISHED'}

class OBJECT_OT_AI_Pick_Video_Model(Operator):
    bl_idname = "object.ai_pick_video_model"
    bl_label = "选择视频模型"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        return {'FINISHED'}

    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        props = context.scene.ai_gen_props
        col = layout.column(align=True)
        col.label(text="选择视频生成模型:", icon='SEQUENCE')
        col.separator()
        if len(props.available_video_models) > 0:
            for item in props.available_video_models:
                is_sel = (props.selected_video_model_id == item.model_id)
                row = col.row()
                if is_sel:
                    row.enabled = False
                    row.label(text="✓ " + item.model_id, icon='CHECKMARK')
                else:
                    op = row.operator("object.ai_set_video_model", text=item.model_id, icon='SEQUENCE')
                    op.model_id = item.model_id
                if item.owned_by:
                    row.label(text=item.owned_by, icon='DOT')
        else:
            col.label(text="(无可用模型 · 请先点击「获取视频模型」)", icon='ERROR')
        col.separator()

class OBJECT_OT_AI_Set_Video_Model(Operator):
    bl_idname = "object.ai_set_video_model"
    bl_label = "设置视频模型"
    model_id: StringProperty(name="模型ID", default="")

    def execute(self, context):
        props = context.scene.ai_gen_props
        mid = self.model_id.strip()
        if not mid:
            return {'CANCELLED'}
        props.selected_video_model_id = mid
        props.video_api_url = build_video_api_url(props.video_platform_url, mid)
        props.video_model_id_manual = mid
        self.report({'INFO'}, "视频模型 → " + mid)
        return {'FINISHED'}

class OBJECT_OT_AI_Toggle_Section(Operator):
    bl_idname = "object.ai_toggle_section"
    bl_label = "折叠/展开"
    bl_options = {'REGISTER', 'INTERNAL'}

    prop_name: StringProperty(name="属性名", default="")

    def execute(self, context):
        props = context.scene.ai_gen_props
        name = self.prop_name.strip()
        if not name or not hasattr(props, name):
            return {'CANCELLED'}
        setattr(props, name, not getattr(props, name))
        return {'FINISHED'}

class AI_Generate_Model_UIList(bpy.types.UIList):
    bl_idname = "AI_Generate_Model_UIList"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            if item.model_type == "image":
                layout.label(text=item.model_id, icon='IMAGE_DATA')
            else:
                layout.label(text=item.model_id, icon='TEXT')
        elif self.layout_type == 'GRID':
            if item.model_type == "image":
                layout.label(text=item.model_id, icon='IMAGE_DATA')
            else:
                layout.label(text=item.model_id, icon='TEXT')

class AI_Generate_Video_Model_UIList(bpy.types.UIList):
    bl_idname = "AI_Generate_Video_Model_UIList"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.model_id, icon='SEQUENCE')
        elif self.layout_type == 'GRID':
            layout.label(text=item.model_id, icon='SEQUENCE')

class VIEW3D_PT_AI_Generate_Panel(Panel):
    bl_label = "摄像机视图 AI 渲染图/视频生成器 v4.6"
    bl_idname = "VIEW3D_PT_ai_generate_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AI生成'

    def draw(self, context):
        layout = self.layout
        props = context.scene.ai_gen_props

        # ================================================================
        # 区块一：生成图片（可折叠）
        # ================================================================
        box = layout.box()
        header = box.row(align=True)
        icon_img = 'TRIA_DOWN' if props.image_section_expanded else 'TRIA_RIGHT'
        header.operator("object.ai_toggle_section", text="", icon=icon_img, emboss=False).prop_name = "image_section_expanded"
        header.label(text="生成图片", icon='RENDER_RESULT')

        if props.image_section_expanded:
            if props.is_generating or props.progress_text:
                row = box.row(align=True)
                row.label(text=props.progress_text or "就绪", icon='INFO')

            box.separator()
            col = box.column(align=True)
            col.label(text="图片 API:", icon='URL')
            col.prop(props, "image_platform")
            if props.image_platform == "sd_webui":
                col.prop(props, "image_api_url", text="接口地址")
                col.label(text="本地 SD WebUI 无需 Token", icon='INFO')
            elif props.image_platform == "comfyui":
                col.prop(props, "image_platform_url", text="ComfyUI 地址")
                col.prop(props, "image_comfyui_workflow_file", text="图片工作流文件")
                col.label(text="工作流需用 ComfyUI 右键→保存(API 格式)", icon='INFO')
            else:
                col.prop(props, "image_platform_url", text="网站地址")
                col.prop(props, "api_token")
                col.prop(props, "model_filter", text="过滤")

            # --- 图像模型选择（原生下拉框，ComfyUI 下可选） ---
            row = box.row(align=True)
            row.prop(props, "selected_image_model_id", text="图像模型")
            row.operator("object.ai_fetch_models", text="获取", icon='FILE_REFRESH')
            if props.image_platform == "api":
                box.prop(props, "image_api_url", text="API URL")
            elif props.image_platform == "comfyui":
                col.label(text="（ComfyUI：模型可选，工作流自带模型时留空即可）", icon='INFO')
            # sd_webui 已在上方单独显示 API 地址

            box.separator()
            col = box.column(align=True)
            col.label(text="提示词:", icon='TEXT')
            col.prop(props, "use_scene_object_names", text="用场景物体名作为内容")
            if props.use_scene_object_names and props.scene_object_names:
                col.label(text=f"捕获到: {props.scene_object_names}", icon='OUTLINER_OB_MESH')
            row = box.row(align=True)
            row.prop(props, "prompt_content", text="内容")
            row = box.row(align=True)
            row.prop(props, "prompt_color", text="色彩")
            row = box.row(align=True)
            row.prop(props, "prompt_reference", text="参考")
            row.prop(props, "prompt_other", text="其他")

            # 风格参考图上传 + 缩略图预览
            col = box.column(align=True)
            row = col.row(align=True)
            row.operator("object.ai_load_style_ref", text="上传风格参考图", icon='IMAGE_DATA')
            if props.style_ref_image:
                row.operator("object.ai_clear_style_ref", text="", icon='X')
            if props.style_ref_image and os.path.exists(props.style_ref_image):
                # 显示参考图缩略图预览
                _pcoll = _get_style_preview_collection()
                thumb_key = props.style_ref_image
                if thumb_key not in _pcoll:
                    try:
                        _pcoll.load(thumb_key, props.style_ref_image, 'IMAGE')
                    except Exception:
                        pass
                if thumb_key in _pcoll:
                    col.template_icon(icon_value=_pcoll[thumb_key].icon_id, scale=6.0)
                    col.label(text=os.path.basename(props.style_ref_image), icon='FILE_IMAGE')
                else:
                    col.label(text="已选: " + os.path.basename(props.style_ref_image), icon='FIXED_SIZE')
            elif props.style_ref_image:
                col.label(text="已选(文件不存在): " + os.path.basename(props.style_ref_image), icon='ERROR')

            box.separator()
            col = box.column(align=True)
            col.label(text="输出:", icon='MODIFIER')
            row = col.row(align=True)
            row.prop(props, "follow_camera_aspect", text="跟随镜头")
            row.prop(props, "image_size", text="分辨率")
            col.prop(props, "denoising_strength", slider=True, text="重绘强度")
            col.prop(props, "auto_save_image", text="自动保存")
            if props.optimize_ref_image:
                row = col.row(align=True)
                row.prop(props, "ref_image_max_size", text="最大边长")
                row.prop(props, "ref_image_quality", text="质量")

            box.separator()
            box.operator("object.render_ref_preview", text="渲染预览", icon='IMAGE_REFERENCE')

            box.separator()
            col = box.column(align=True)
            # ComfyUI 以工作流文件为准，其他平台才要求选择图像模型
            can_generate = (
                (props.image_platform == "comfyui" and props.image_comfyui_workflow_file.strip() and os.path.exists(props.image_comfyui_workflow_file))
                or (props.image_platform != "comfyui" and _has_selected_image_model(props))
            )
            if props.is_generating or not can_generate:
                col.enabled = False
            col.operator("object.ai_generate_async", text="▶ 生成图片", icon='RENDER_RESULT')
            if not can_generate:
                if props.image_platform == "comfyui":
                    col.label(text="请先选择图片 ComfyUI 工作流文件", icon='INFO')
                else:
                    col.label(text="请先在上方选择图像模型", icon='INFO')
            if props.last_image_path:
                col.operator("object.ai_show_last_image", text="重新显示最近生成图", icon='IMAGE_DATA')

        # ================================================================
        # 区块二：生成视频（可折叠）
        # ================================================================
        box = layout.box()
        header = box.row(align=True)
        icon_vid = 'TRIA_DOWN' if props.video_section_expanded else 'TRIA_RIGHT'
        header.operator("object.ai_toggle_section", text="", icon=icon_vid, emboss=False).prop_name = "video_section_expanded"
        header.label(text="生成视频", icon='RENDER_ANIMATION')

        if props.video_section_expanded:
            if props.is_generating_video or props.progress_text_video:
                row = box.row(align=True)
                row.label(text=props.progress_text_video or "视频就绪", icon='SEQUENCE')

            # --- 视频 API 设置（与图片区独立） ---
            box.separator()
            col = box.column(align=True)
            col.label(text="视频 API:", icon='URL')
            col.prop(props, "video_platform")
            if props.video_platform == "sd_webui":
                col.prop(props, "video_api_url", text="接口地址")
                col.label(text="本地 SD WebUI 无需 Token", icon='INFO')
            elif props.video_platform == "comfyui":
                col.prop(props, "video_platform_url", text="ComfyUI 地址")
                col.prop(props, "video_comfyui_workflow_file", text="视频工作流文件")
                col.label(text="工作流用 ComfyUI 右键→保存(API 格式)，需含 {prompt} 占位符", icon='INFO')
            else:
                col.prop(props, "video_platform_url", text="网站地址")
                col.prop(props, "api_token")
                col.prop(props, "model_filter", text="过滤")

            # --- 视频模型选择（原生下拉框） ---
            row = box.row(align=True)
            row.prop(props, "selected_video_model_id", text="视频模型")
            row.operator("object.ai_fetch_video_models", text="获取", icon='FILE_REFRESH')

            # --- 参数 ---
            box.separator()
            col = box.column(align=True)
            col.label(text="参数:", icon='PREVIEW_RANGE')
            row = col.row(align=True)
            row.prop(props, "video_duration", text="时长(秒)")
            if props.video_platform != "comfyui":
                col.prop(props, "use_ref_image", text="使用渲染参考图(图生视频)")
                col.prop(props, "ref_image_url", text="参考图网络地址(可选)")
                col.prop(props, "use_style_ref", text="使用风格参考图")
                col.prop(props, "style_ref_url", text="风格图网络地址(可选)")
                col.label(text="若中继站拒绝 data URL(>1024字符)，请填已托管短URL或关闭参考图", icon='INFO')
            col.prop(props, "auto_save_video", text="自动保存视频")
            col.prop(props, "video_save_dir", text="保存位置")
            col.label(text="留空 → 保存到 Blender 文件所在文件夹", icon='FILE_FOLDER')

            # 视频提示词 & 镜头动画参考
            box.separator()
            col = box.column(align=True)
            col.label(text="提示词:", icon='TEXT')
            col.prop(props, "video_prompt", text="")
            col.prop(props, "video_gen_mode", text="视频参考方式")
            if props.video_gen_mode == "first_last":
                col.label(text="首尾帧取自相机动画的第一帧与最后一帧", icon='IMAGE_DATA')
            else:
                row = col.row(align=True)
                row.prop(props, "ref_video_file", text="参考视频文件")
                if props.ref_video_file and os.path.exists(props.ref_video_file):
                    col.label(text="将以 base64 形式随 JSON 上传，作为镜头运动参考", icon='FILE_MOVIE')
                else:
                    col.label(text="留空则用镜头动画；选本地 mp4 直接当参考", icon='INFO')
                if props.video_platform != "comfyui":
                    col.prop(props, "ref_video_url", text="参考视频网络地址(可选)")

            # 风格参考图（视频也支持，与图片区共享同一张图）
            if props.video_platform != "comfyui":
                box.separator()
                col = box.column(align=True)
                row = col.row(align=True)
                row.operator("object.ai_load_style_ref", text="上传风格参考图", icon='IMAGE_DATA')
                if props.style_ref_image:
                    row.operator("object.ai_clear_style_ref", text="", icon='X')
                if props.style_ref_image and os.path.exists(props.style_ref_image):
                    _pcoll = _get_style_preview_collection()
                    thumb_key = props.style_ref_image
                    if thumb_key not in _pcoll:
                        try:
                            _pcoll.load(thumb_key, props.style_ref_image, 'IMAGE')
                        except Exception:
                            pass
                    if thumb_key in _pcoll:
                        col.template_icon(icon_value=_pcoll[thumb_key].icon_id, scale=6.0)
                        col.label(text=os.path.basename(props.style_ref_image), icon='FILE_IMAGE')
                    else:
                        col.label(text="已选: " + os.path.basename(props.style_ref_image), icon='FIXED_SIZE')
                elif props.style_ref_image:
                    col.label(text="已选(文件不存在): " + os.path.basename(props.style_ref_image), icon='ERROR')

            box.separator()
            col = box.column(align=True)
            if props.is_generating_video:
                # 生成中：显示取消按钮（可点击重置卡死状态）
                col.operator("object.ai_cancel_video", text="✕ 取消 / 重置", icon='PAUSE')
            else:
                block_reason = None
                if props.video_platform == "comfyui":
                    if not props.video_comfyui_workflow_file.strip():
                        block_reason = "请先在上方选择视频 ComfyUI 工作流文件"
                elif not _has_selected_video_model(props):
                    block_reason = "请先在上方选择视频模型"
                if props.is_generating_video or block_reason:
                    col.enabled = False
                col.operator("object.ai_generate_video_async", text="▶ 生成视频", icon='RENDER_ANIMATION')
                col.enabled = True
                if block_reason:
                    col.label(text=block_reason, icon='INFO')
            if props.last_video_path:
                row = col.row(align=True)
                row.operator("object.ai_show_last_video", text="打开最近视频", icon='PLAY')
                row.operator("object.ai_open_video_folder", text="打开文件夹", icon='FILE_FOLDER')

        # 失败详情（底部）
        if len(props.fail_details) > 0:
            box = layout.box()
            row = box.row(align=True)
            row.label(text=f"图片失败:", icon='ERROR')
            row.operator("object.ai_copy_errors", text="", icon='COPYDOWN')
            row.operator("object.ai_clear_fails", text="", icon='X')
            for f in props.fail_details[:3]:
                ccol = box.column(align=True)
                ccol.label(text=f"  X {f.obj_name}", icon='MESH_DATA')
                for line in f.error.split('\n')[:3]:
                    ccol.label(text=f"    {line[:90]}", icon='DOT')

        if len(props.fail_details_video) > 0:
            box = layout.box()
            row = box.row(align=True)
            row.label(text=f"视频失败:", icon='ERROR')
            row.operator("object.ai_copy_video_errors", text="", icon='COPYDOWN')
            row.operator("object.ai_clear_video_fails", text="", icon='X')
            for f in props.fail_details_video[:3]:
                ccol = box.column(align=True)
                ccol.label(text=f"  X {f.obj_name}", icon='SEQUENCE')
                for line in f.error.split('\n')[:3]:
                    ccol.label(text=f"    {line[:90]}", icon='DOT')


classes = (
    AI_Generate_FailItem,
    AI_Generate_ModelItem,
    AI_Generate_VideoModelItem,
    AI_Generate_Properties,
    OBJECT_OT_Render_Ref_Preview,
    OBJECT_OT_AI_Fetch_Models,
    OBJECT_OT_AI_Apply_Selected_Model,
    OBJECT_OT_AI_Generate_Async,
    OBJECT_OT_AI_Show_Last_Image,
    OBJECT_OT_AI_Copy_Errors,
    OBJECT_OT_AI_Clear_Fails,
    OBJECT_OT_AI_Load_Style_Ref,
    OBJECT_OT_AI_Clear_Style_Ref,
    OBJECT_OT_AI_Fetch_Video_Models,
    OBJECT_OT_AI_Apply_Video_Model,
    OBJECT_OT_AI_Generate_Video_Async,
    OBJECT_OT_AI_Cancel_Video,
    OBJECT_OT_AI_Show_Last_Video,
    OBJECT_OT_AI_Open_Video_Folder,
    OBJECT_OT_AI_Copy_Video_Errors,
    OBJECT_OT_AI_Clear_Video_Fails,
    OBJECT_OT_AI_Toggle_Section,
    AI_Generate_Model_UIList,
    AI_Generate_Video_Model_UIList,
    VIEW3D_PT_AI_Generate_Panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.ai_gen_props = PointerProperty(type=AI_Generate_Properties)


def unregister():
    _release_style_preview_collection()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "ai_gen_props"):
        del bpy.types.Scene.ai_gen_props


if __name__ == "__main__":
    register()
