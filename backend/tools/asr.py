"""ASR 工具：通过 Triton gRPC 调用内网 ASR 服务，全局并发上限 5。

接口说明：
  服务端: 由 TRITON_SERVER 环境变量指定（host:port）
  模型:   asr
  headers: service_id=5000, sub_service_id=21
  输入:    speech (WAV 文件字节 base64)，设 voice_resample=True 自动重采样
  输出:    text (识别结果), code (200=成功)
"""
import base64
import os
import threading
import uuid
import logging

import numpy as np

logger = logging.getLogger(__name__)

# ── 全局并发限制：所有场景共用，最多同时 5 个 ASR 请求 ────────────────────────
_SEMAPHORE = threading.Semaphore(5)

# ── Triton 客户端（延迟初始化，全局复用）────────────────────────────────────────
_triton_client = None
_client_lock   = threading.Lock()

_SERVER  = os.getenv("TRITON_SERVER", "")
_MODEL   = "asr"
_HEADERS = {"service_id": "5000", "sub_service_id": "21"}


def _get_client():
    global _triton_client
    with _client_lock:
        if _triton_client is None:
            import tritonclient.grpc as grpcclient
            keepalive = grpcclient.KeepAliveOptions(
                keepalive_time_ms=2**31 - 1,
                keepalive_timeout_ms=20000,
                keepalive_permit_without_calls=False,
                http2_max_pings_without_data=2,
            )
            _triton_client = grpcclient.InferenceServerClient(
                url=_SERVER, verbose=False, keepalive_options=keepalive
            )
    return _triton_client


def _reset_client():
    global _triton_client
    with _client_lock:
        _triton_client = None


def _call_asr_api(audio_path: str) -> str:
    """实际 API 调用，调用方需持有 _SEMAPHORE。"""
    import tritonclient.grpc as grpcclient

    with open(audio_path, "rb") as f:
        audio_bytes = f.read()
    speech_b64 = base64.b64encode(audio_bytes).decode("ascii")

    client    = _get_client()
    trace_id  = uuid.uuid4().hex

    # 输入构造（参照参考代码，WAV 整文件 base64 + 重采样）
    speech_np     = np.array([[speech_b64.encode("utf-8")]])          # (1,1) BYTES
    fmt_np        = np.array([[b"pcm"]])                              # (1,1) BYTES
    resample_np   = np.array([[True]],  dtype=bool)                   # (1,1) BOOL
    frame_len_np  = np.array([[]],      dtype=np.int32)               # (1,0) INT32 - pcm 无需 opus 帧列表
    frame_size_np = np.array([[0]],     dtype=np.int32)               # (1,1) INT32
    add_punc_np   = np.array([[True]],  dtype=bool)                   # (1,1) BOOL

    inputs = [
        grpcclient.InferInput("speech",            speech_np.shape,     "BYTES").set_data_from_numpy(speech_np),
        grpcclient.InferInput("voice_format",      fmt_np.shape,        "BYTES").set_data_from_numpy(fmt_np),
        grpcclient.InferInput("voice_resample",    resample_np.shape,   "BOOL" ).set_data_from_numpy(resample_np),
        grpcclient.InferInput("opus_frame_len_list", frame_len_np.shape, "INT32").set_data_from_numpy(frame_len_np),
        grpcclient.InferInput("opus_frame_size",   frame_size_np.shape, "INT32").set_data_from_numpy(frame_size_np),
        grpcclient.InferInput("add_punc",          add_punc_np.shape,   "BOOL" ).set_data_from_numpy(add_punc_np),
    ]
    outputs = [grpcclient.InferRequestedOutput(k) for k in ("text", "code", "message")]

    try:
        resp = client.infer(_MODEL, inputs, request_id=trace_id,
                            outputs=outputs, headers=_HEADERS)
    except Exception as e:
        _reset_client()   # 连接异常时重置，下次重建
        raise RuntimeError(f"ASR gRPC 连接失败: {e}") from e

    resp_id = resp.get_response().id
    if resp_id == trace_id:
        code = int(resp.as_numpy("code")[0])
        if code == 200:
            return resp.as_numpy("text")[0].decode("utf-8")
        msg = resp.as_numpy("message")[0].decode("utf-8")
        raise RuntimeError(f"ASR 识别失败 code={code}: {msg}")
    elif resp_id == "-1":
        raise RuntimeError("ASR 被限流，请稍后重试")
    else:
        raise RuntimeError(f"ASR 网关异常 (response_id={resp_id})")


def transcribe(audio_path: str) -> str:
    """转写单条音频，全局并发受 Semaphore(5) 限制。"""
    with _SEMAPHORE:
        try:
            text = _call_asr_api(audio_path)
            logger.debug("ASR ok: %s → %s", audio_path, text[:40])
            return text
        except Exception as e:
            logger.warning("ASR failed for %s: %s", audio_path, e)
            return ""   # 失败返回空字符串，不影响主流程


def transcribe_batch(audio_paths: list[str]) -> dict[str, str]:
    """批量转写，每条独立限流（各自占用一个并发槽）。"""
    results = {}
    for path in audio_paths:
        results[path] = transcribe(path)
    return results
