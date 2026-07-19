bl_info = {
    "name": "摄像机视图 AI 渲染图生成器",
    "author": "AI Assistant",
    "version": (4, 5, 0),
    "blender": (3, 0, 0),
    "location": "3D视图 → 侧边栏(N键) → AI生成",
    "description": "渲染场景视图作为底图，调用 AI API 生成一张图片，显示在图像编辑器",
    "category": "Object",
}

import bpy
import os
import base64
import queue
import threading
import tempfile
import time
import requests
from bpy.props import (
    StringProperty, BoolProperty, IntProperty, FloatProperty,
    PointerProperty, EnumProperty, CollectionProperty,
)
from bpy.types import Operator, Panel, PropertyGroup

# =============================================================================
# 一、平台配置
# =============================================================================

PLATFORM_CHOICES = [
    ("sd_webui", "本地 SD WebUI",  "本地 Stable Diffusion WebUI 服务",  0),
    ("api",      "各种 API",       "通用 fal-ai 兼容 API 转发站：输入网站+Token 后获取模型列表",  1),
]

SD_WEBUI_DEFAULT_URL = "http://127.0.0.1:7860/sdapi/v1/img2img"
API_DEFAULT_URL = "https://yunwu.ai"


def build_api_url_for_model(platform_url, model_id):
    """根据选中的模型 ID 自动构造完整 API URL"""
    base = platform_url.strip().rstrip('/')
    if not base.startswith(('http://', 'https://')):
        base = 'https://' + base
    if not model_id:
        return base
    if '/' in model_id:
        return f"{base}/{model_id.lstrip('/')}/image-to-image"
    else:
        return f"{base}/v1/images/edits"


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
    filtered = []
    for m in models:
        is_img, _ = is_image_generation_model(m["id"])
        if not is_img:
            continue
        is_gg, _ = is_gpt_or_google_model(m["id"], m.get("owned_by", ""))
        if is_gg:
            filtered.append(m)
    return filtered


def fetch_models_from_api(platform_url, api_token):
    """从平台拉取可用模型列表（OpenAI 兼容 /v1/models）"""
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


def image_to_base64(image_path):
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode('utf-8')


def image_to_data_url(image_path):
    """把图片转成 data URL，自动识别 MIME（JPEG 比 PNG 小很多，省上下文）"""
    ext = os.path.splitext(image_path)[1].lower()
    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
    b64 = image_to_base64(image_path)
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
                   denoising, steps, cfg_scale, image_size="1024x1024"):
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
              denoising, steps, cfg_scale, image_size=""):
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
                             image_size="1024x1024"):
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
    try:
        ext = os.path.splitext(ref_image_path)[1].lower()
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
        fname = f"ref{ext}" if ext else "ref.png"
        with open(ref_image_path, "rb") as f:
            files = {"image": (fname, f, mime)}
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
    is_html, err = _detect_html(resp)
    if is_html:
        return {"_error": err}
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return {"_error": f"API 返回非 JSON 响应: {resp.text[:200]}"}


def _parse_image_response(result):
    """统一解析多种返回格式"""
    if isinstance(result, dict) and "_error" in result:
        return None, result["_error"]
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


def _download_to_temp(url):
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(r.content)
        tmp.close()
        return tmp.name, None
    except Exception as e:
        return None, f"下载生成图失败: {e}"


def _detect_protocol(platform, api_url, model_id=""):
    """根据平台、URL、模型 ID 智能判断协议"""
    if platform == "sd_webui":
        return "sd_webui"
    url_lower = api_url.lower()
    if "/v1/images/edits" in url_lower or "/v1/images/generations" in url_lower:
        return "openai"
    if model_id and "/" not in model_id:
        return "openai"
    return "fal"


def call_api(platform, api_url, api_token, ref_image_path, prompt,
             negative_prompt, retry=2, model_id="", image_size="1024",
             denoising=0.75, steps=20, cfg_scale=7.0):
    """统一 API 入口"""
    protocol = _detect_protocol(platform, api_url, model_id)
    last_err = None
    for attempt in range(retry + 1):
        try:
            if protocol == "sd_webui":
                b64 = image_to_base64(ref_image_path)
                result = _call_sd_webui(api_url, api_token, b64, prompt,
                                        negative_prompt, denoising, steps, cfg_scale)
            elif protocol == "openai":
                size_str = f"{image_size}x{image_size}" if image_size and str(image_size).isdigit() else "1024x1024"
                result = _call_openai_image_edit(api_url, api_token, ref_image_path, prompt,
                                                  negative_prompt, model_id=model_id, image_size=size_str)
            else:
                data_url = image_to_data_url(ref_image_path)
                result = _call_fal(api_url, api_token, data_url, prompt,
                                   negative_prompt, denoising, steps, cfg_scale, image_size)
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
        selected_model_id = ""
        if 0 <= props.selected_model_index < len(props.available_models):
            selected_model_id = props.available_models[props.selected_model_index].model_id
        return {
            "platform": props.platform,
            "platform_url": props.platform_url,
            "api_url": props.api_url,
            "api_token": props.api_token,
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
        )

        if err:
            self.result_queue.put({"type": "error", "error": err})
        else:
            self.result_queue.put({"type": "success", "img_path": img_path})

        self.result_queue.put({"type": "finished"})


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


class AI_Generate_Properties(PropertyGroup):
    # --- 平台选择 ---
    platform: EnumProperty(
        name="平台",
        items=PLATFORM_CHOICES,
        update=lambda self, context: self._on_platform_change(context),
    )

    def _on_platform_change(self, context):
        if self.platform == "sd_webui":
            self.api_url = SD_WEBUI_DEFAULT_URL
        else:
            if "127.0.0.1" in self.api_url or not self.api_url.strip():
                self.api_url = API_DEFAULT_URL
            if not self.platform_url.strip():
                self.platform_url = API_DEFAULT_URL

    platform_url: StringProperty(name="网站地址", default=API_DEFAULT_URL)
    api_url: StringProperty(name="API 完整地址", default=SD_WEBUI_DEFAULT_URL)
    api_token: StringProperty(name="API Token", default="", subtype="PASSWORD")

    # 动态拉取的模型列表
    available_models: CollectionProperty(type=AI_Generate_ModelItem)
    selected_model_index: IntProperty(default=0)
    models_fetch_status: StringProperty(name="拉取状态", default="")

    # 模型过滤
    model_filter: EnumProperty(
        name="模型过滤",
        items=[
            ("gpt_google", "仅 GPT/Google 图像模型", "只显示 OpenAI 和 Google 的图像生成模型",  0),
            ("all_image",  "所有图生图模型",         "显示所有识别为图生图的可选模型",            1),
            ("all",        "显示全部",               "不做过滤（列表很长）",                       2),
        ],
        default="gpt_google",
    )

    # --- 自定义提示词 ---
    prompt_content: StringProperty(
        name="内容",
        description="画面主体描述：物体、场景、动作等核心内容",
        default="",
    )
    prompt_color: StringProperty(
        name="色彩",
        description="色调与氛围：冷暖、明暗、主色等色彩描述",
        default="",
    )
    prompt_reference: StringProperty(
        name="参考",
        description="风格参考：电影名、艺术家、画风等（如：沙丘电影、赛博朋克）",
        default="",
    )
    prompt_other: StringProperty(
        name="其他",
        description="额外参数：画质、镜头、构图等补充描述",
        default="photorealistic, high detail, cinematic lighting",
    )

    # 输出尺寸 / 镜头比例
    follow_camera_aspect: BoolProperty(
        name="跟随镜头比例",
        description="输出图比例自动匹配摄像机视图（推荐：保证构图与镜头布局一致）",
        default=True,
    )
    image_size: StringProperty(
        name="输出分辨率(长边)",
        description="长边像素数。跟随镜头比例时按此推导宽高；关闭时作为正方形边长",
        default="1536",
    )
    # 内部：每次生成时根据镜头比例计算出的实际 WxH 字符串
    computed_size: StringProperty(default="")

    # 重绘强度：越低越贴合镜头布局
    denoising_strength: FloatProperty(
        name="重绘强度",
        description="图生图重绘强度。越低越保留镜头布局(0.2≈几乎不变, 1.0≈大幅重绘)。"
                    "仅 API / SD WebUI 生效；OpenAI(gpt-image-2) 不支持，靠参考图本身保布局",
        default=0.5, min=0.1, max=1.0,
    )

    # 用场景物体名作为生成内容（命名驱动）
    use_scene_object_names: BoolProperty(
        name="用场景物体名作为内容",
        description="把场景里所有网格物体的名字喂给 AI，让生成的图里物体变成名字对应的真实物品"
                    "（保留整场景镜头构图，不是单独渲染某个模型）",
        default=True,
    )
    # 内部：每次生成时收集到的场景物体名（逗号分隔）
    scene_object_names: StringProperty(default="")

    # --- 上下文优化 ---
    optimize_ref_image: BoolProperty(
        name="压缩参考图",
        description="发送前缩放并转 JPEG，显著减少 API 上下文消耗与费用",
        default=True,
    )
    ref_image_max_size: IntProperty(
        name="参考图最大边长",
        description="发送前参考图缩放到的最大像素边长（0 = 不缩放）",
        default=1024, min=0, max=2048,
    )
    ref_image_quality: IntProperty(
        name="JPEG 质量",
        description="压缩质量（10-100），越低体积越小",
        default=85, min=10, max=100,
    )

    # 是否自动保存生成图到本地文件夹
    auto_save_image: BoolProperty(
        name="自动保存到本地",
        description="生成后把图片复制到 ~/AI_Generated_Images/ 目录",
        default=True,
    )

    # --- 运行时状态 ---
    is_generating: BoolProperty(default=False)
    progress_text: StringProperty(default="")
    last_error: StringProperty(default="")
    fail_details: CollectionProperty(type=AI_Generate_FailItem)
    last_image_path: StringProperty(name="最近生成图", default="")


# =============================================================================
# 六、操作符
# =============================================================================

class OBJECT_OT_Render_Ref_Preview(Operator):
    """预览摄像机视图"""
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
    """获取模型列表"""
    bl_idname = "object.ai_fetch_models"
    bl_label = "获取模型列表"
    bl_options = {'REGISTER'}

    def execute(self, context):
        props = context.scene.ai_gen_props
        if not props.platform_url.strip():
            self.report({'ERROR'}, "请先填写网站地址")
            return {'CANCELLED'}
        if not props.api_token.strip():
            self.report({'ERROR'}, "请先填写 API Token")
            return {'CANCELLED'}

        self.report({'INFO'}, f"正在从 {props.platform_url} 获取模型列表...")
        props.models_fetch_status = "拉取中..."

        models, err = fetch_models_from_api(props.platform_url, props.api_token)
        if err:
            self.report({'ERROR'}, f"获取失败: {err}")
            props.models_fetch_status = f"✗ {err[:60]}"
            props.last_error = err
            return {'CANCELLED'}

        total_count = len(models)
        models = filter_models_list(models, props.model_filter)
        filtered_count = len(models)

        if filtered_count == 0:
            props.available_models.clear()
            props.models_fetch_status = f"✗ 网站共 {total_count} 个模型，无匹配"
            self.report({'WARNING'}, f"网站共 {total_count} 个模型，但过滤后无匹配 — 尝试其他过滤模式")
            return {'CANCELLED'}

        props.available_models.clear()
        for m in models:
            item = props.available_models.add()
            item.model_id = m["id"]
            item.owned_by = m["owned_by"]
            is_img, _ = is_image_generation_model(m["id"])
            item.model_type = "image" if is_img else "text"

        props.models_fetch_status = f"✓ 显示 {filtered_count}/{total_count} 个模型"
        self.report({'INFO'}, f"获取到 {total_count} 个，过滤后 {filtered_count} 个")

        for i, item in enumerate(props.available_models):
            if item.model_type == "image":
                props.selected_model_index = i
                self._apply_selected_model(props)
                self.report({'INFO'}, f"自动选中: {item.model_id}")
                break
        return {'FINISHED'}

    @staticmethod
    def _apply_selected_model(props):
        if 0 <= props.selected_model_index < len(props.available_models):
            item = props.available_models[props.selected_model_index]
            props.api_url = build_api_url_for_model(props.platform_url, item.model_id)


class OBJECT_OT_AI_Apply_Selected_Model(Operator):
    """应用选中的模型到 API URL"""
    bl_idname = "object.ai_apply_selected_model"
    bl_label = "应用选中模型"

    def execute(self, context):
        props = context.scene.ai_gen_props
        if not (0 <= props.selected_model_index < len(props.available_models)):
            self.report({'WARNING'}, "请先在列表中选择一个模型")
            return {'CANCELLED'}
        OBJECT_OT_AI_Fetch_Models._apply_selected_model(props)
        item = props.available_models[props.selected_model_index]
        if item.model_type != "image":
            self.report({'WARNING'}, f"⚠ {item.model_id} 看起来不是图生图模型")
        else:
            self.report({'INFO'}, f"已应用: {item.model_id}")
        return {'FINISHED'}


class OBJECT_OT_AI_Generate_Async(Operator):
    """开始 AI 生成：渲染场景视图 → 调用 API → 显示生成的图"""
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
        if not props.api_url.strip():
            self.report({'ERROR'}, "API 地址不能为空")
            return {'CANCELLED'}

        # 渲染场景视图作为参考底图
        ref_path, err = render_camera_view_to_temp(resolution_percentage=50)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}

        # 计算输出尺寸：跟随镜头比例时按摄像机分辨率推导宽高，保证构图与镜头一致
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
            props.computed_size = f"{w}x{h}"
        else:
            props.computed_size = f"{base}x{base}"

        # 收集场景中所有网格物体名，作为命名驱动的生成内容
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
                    # 在图像编辑器显示
                    try:
                        img = bpy.data.images.load(img_path, check_existing=True)
                        shown = False
                        for area in context.screen.areas:
                            if area.type == 'IMAGE_EDITOR':
                                area.spaces.active.image = img
                                shown = True
                                break
                        if not shown:
                            self.report({'WARNING'}, "未找到图像编辑器，请手动打开查看")
                        else:
                            self.report({'INFO'}, "✓ 生成成功，已显示在图像编辑器")
                    except Exception as e:
                        self.report({'ERROR'}, f"加载图片失败: {e}")
                    # 自动保存到本地
                    if props.auto_save_image:
                        saved = save_generated_image(img_path)
                        if saved:
                            self.report({'INFO'}, f"已保存到: {saved}")
                    props.progress_text = "✓ 生成完成（看图像编辑器）"
                    context.area.tag_redraw()
                elif mtype == "error":
                    err = msg.get("error", "未知错误")
                    props.last_error = err
                    props.progress_text = f"✗ 失败: {err[:60]}"
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
    """重新显示最近一次生成的图（在图像编辑器中）"""
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
            self.report({'ERROR'}, f"加载失败: {e}")
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
            "AI 渲染图生成器 - 失败详情",
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"平台: {props.platform}",
            f"网站: {props.platform_url}",
            f"API URL: {props.api_url}",
            f"模型: {props.available_models[props.selected_model_index].model_id if 0 <= props.selected_model_index < len(props.available_models) else '(未选)'}",
            "",
        ]
        for f in props.fail_details:
            lines.append(f"✗ {f.obj_name}")
            lines.append(f"    {f.error}")
            lines.append("")
        text = "\n".join(lines)
        try:
            context.window_manager.clipboard = text
            self.report({'INFO'}, f"已复制 {len(props.fail_details)} 条错误到剪贴板")
        except Exception as e:
            self.report({'ERROR'}, f"复制失败: {e}")
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


# =============================================================================
# 七、UI 列表
# =============================================================================

class AI_Generate_Model_UIList(bpy.types.UIList):
    bl_idname = "AI_Generate_Model_UIList"

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_property, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            if item.model_type == "image":
                row.label(text=item.model_id, icon='IMAGE_DATA')
            elif item.model_type == "text":
                row.label(text=item.model_id, icon='TEXT')
            else:
                row.label(text=item.model_id, icon='QUESTION')
            if item.owned_by:
                row.label(text=f"  [{item.owned_by}]", icon='DOT')
        elif self.layout_type == 'GRID':
            if item.model_type == "image":
                layout.label(text=item.model_id, icon='IMAGE_DATA')
            else:
                layout.label(text=item.model_id, icon='TEXT')


# =============================================================================
# 八、UI 面板
# =============================================================================

class VIEW3D_PT_AI_Generate_Panel(Panel):
    bl_label = "摄像机视图 AI 渲染图生成器 v4.5"
    bl_idname = "VIEW3D_PT_ai_generate_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AI生成'

    def draw(self, context):
        layout = self.layout
        props = context.scene.ai_gen_props

        # 状态条
        if props.is_generating or props.progress_text:
            box = layout.box()
            row = box.row(align=True)
            row.label(text=props.progress_text or "就绪", icon='INFO')

        # API 设置
        box = layout.box()
        box.label(text="API 设置", icon='URL')
        box.prop(props, "platform")

        if props.platform == "sd_webui":
            box.prop(props, "api_url", text="接口地址")
            box.label(text="本地 SD WebUI 无需 Token", icon='INFO')
        else:
            box.prop(props, "platform_url", text="网站地址")
            box.prop(props, "api_token")
            box.prop(props, "model_filter", text="过滤")
            row = box.row(align=True)
            row.operator("object.ai_fetch_models", text="获取模型列表", icon='FILE_REFRESH')
            if props.models_fetch_status:
                box.label(text=props.models_fetch_status, icon='INFO')
            if len(props.available_models) > 0:
                box.label(text=f"可用模型 ({len(props.available_models)}):", icon='OUTLINER_OB_IMAGE')
                box.template_list(
                    "AI_Generate_Model_UIList", "",
                    props, "available_models",
                    props, "selected_model_index", rows=5,
                )
                box.operator("object.ai_apply_selected_model", text="应用此模型", icon='CHECKMARK')
            box.prop(props, "api_url", text="API URL")

        # 自定义提示词
        box = layout.box()
        box.label(text="自定义提示词:", icon='TEXT')
        box.prop(props, "use_scene_object_names", text="用场景物体名作为内容")
        if props.use_scene_object_names and props.scene_object_names:
            col = box.column(align=True)
            col.label(text=f"捕获到: {props.scene_object_names}", icon='OUTLINER_OB_MESH')
        box.prop(props, "prompt_content", text="内容")
        box.prop(props, "prompt_color", text="色彩")
        box.prop(props, "prompt_reference", text="参考")
        box.prop(props, "prompt_other", text="其他")
        box.prop(props, "follow_camera_aspect", text="跟随镜头比例")
        box.prop(props, "image_size", text="输出分辨率(长边)")
        col = box.column(align=True)
        col.prop(props, "denoising_strength", slider=True, text="重绘强度")
        col.label(text="越低越贴合镜头布局（OpenAI 模型不支持）", icon='INFO')
        box.prop(props, "auto_save_image")

        # 上下文优化
        box = layout.box()
        box.label(text="上下文优化", icon='MEMORY')
        box.prop(props, "optimize_ref_image", text="压缩参考图 (省上下文)")
        if props.optimize_ref_image:
            row = box.row(align=True)
            row.prop(props, "ref_image_max_size", text="最大边长")
            row.prop(props, "ref_image_quality", text="质量")

        # 参考图预览
        box = layout.box()
        box.label(text="摄像机参考图", icon='CAMERA_DATA')
        box.operator("object.render_ref_preview", text="渲染预览", icon='IMAGE_REFERENCE')

        # 生成
        box = layout.box()
        box.label(text="生成", icon='RENDER_RESULT')
        col = box.column(align=True)
        if props.is_generating:
            col.enabled = False
        col.operator("object.ai_generate_async", text="▶ 生成图片", icon='RENDER_RESULT')
        if props.last_image_path:
            col.operator("object.ai_show_last_image", text="重新显示最近生成图", icon='IMAGE_DATA')

        # 失败详情
        if len(props.fail_details) > 0:
            box = layout.box()
            row = box.row(align=True)
            row.label(text=f"失败详情:", icon='ERROR')
            row.operator("object.ai_copy_errors", text="", icon='COPYDOWN')
            row.operator("object.ai_clear_fails", text="", icon='X')
            for f in props.fail_details[:3]:
                col = box.column(align=True)
                col.label(text=f"  ✗ {f.obj_name}", icon='MESH_DATA')
                for line in f.error.split('\n')[:3]:
                    col.label(text=f"    {line[:90]}", icon='DOT')


# =============================================================================
# 九、注册
# =============================================================================

classes = (
    AI_Generate_FailItem,
    AI_Generate_ModelItem,
    AI_Generate_Properties,
    OBJECT_OT_Render_Ref_Preview,
    OBJECT_OT_AI_Fetch_Models,
    OBJECT_OT_AI_Apply_Selected_Model,
    OBJECT_OT_AI_Generate_Async,
    OBJECT_OT_AI_Show_Last_Image,
    OBJECT_OT_AI_Copy_Errors,
    OBJECT_OT_AI_Clear_Fails,
    AI_Generate_Model_UIList,
    VIEW3D_PT_AI_Generate_Panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.ai_gen_props = PointerProperty(type=AI_Generate_Properties)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "ai_gen_props"):
        del bpy.types.Scene.ai_gen_props


if __name__ == "__main__":
    register()
