# -*- coding: utf-8 -*-
"""模型配置（API Key）接口。

需求：「用户可在任务三平台上快捷输入自己的 API Key」。四个接口：

    GET    /settings/model        查看当前生效配置（key 只回显掩码）
    PUT    /settings/model        保存（字段缺省=不改；空字符串=清除该项）
    POST   /settings/model/test   用当前生效配置做一次最小调用（失败也可读，不抛 500）
    DELETE /settings/model        清空运行时配置，回落环境变量

分层与既有约定一致：本模块**只做参数校验与响应封装**，
读写落在 `ict_parse/runtime_settings.py`（`app/out/platform/llm_settings.json`），
「哪份配置生效」由 `ict_parse/config.py` 决定 —— 也就是抽取链路真正用的那份配置，
所以页面上填的 key 对数据集的 LLM 抽取立刻生效，不需要重启后端。

安全约定（重要）：
    * 响应 / 日志 / 异常信息里**不出现 key 明文**，只出现 `****1234` 形式的掩码；
    * 写盘文件已 chmod 0600（Windows 不支持时忽略）；
    * 配置落在 `app/out/`（已被 .gitignore 排除），密钥不会进版本库。
"""
from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends

from .auth import ROLE_WRITE, require_roles
from .schemas import (ModelGroup, ModelSettingsIn, ModelSettingsOut,
                      ModelTestOut)

log = logging.getLogger('edm.settings')

# 仓库 app/ 目录（内含 ict_parse 包，即任务一/任务二的抽取配置与实现）。
# 追加到 sys.path 末尾而不是开头：app/platform 与标准库 platform 同名，
# 放末尾才不会被抢先命中（pipeline_impl.py 里也是同样的顾虑）。
_APP_DIR = Path(__file__).resolve().parents[3]
if str(_APP_DIR) not in sys.path:
    sys.path.append(str(_APP_DIR))

from ict_parse import config as llm_config            # noqa: E402
from ict_parse import runtime_settings as llm_store   # noqa: E402

router = APIRouter(tags=['模型配置'])

# 「改模型配置」= 写全局状态（页面上填的 key 会被**所有**用户的数据集抽取使用），
# 因此 PUT / POST test / DELETE 三个写端点都要求 admin / reviewer 角色（见 auth.ROLE_WRITE）。
# GET 保持「登录即可」：它只回显掩码，不泄漏明文，普通用户需要看到当前配置状态。
_WRITE_ROLES = Depends(require_roles(*ROLE_WRITE))

# 兜底脱敏：任何疑似 key 的片段都不许出现在响应或日志里
_KEY_RE = re.compile(r'sk-[A-Za-z0-9_\-]{6,}')


def _scrub(text: str, key: str = '') -> str:
    """把疑似 key 的片段替换掉（先精确替换本次用的 key，再按前缀兜底）。"""
    out = text or ''
    if key:
        out = out.replace(key, '****')
    return _KEY_RE.sub('****', out)


def _readable_error(exc: Exception, base: str, key: str) -> str:
    """把 SDK / 网络异常翻译成可读中文；**不泄漏 key**。

    有 HTTP 状态码的分支直接给结论，不拼接原始异常文本（原始文本可能带请求细节）。
    """
    status = getattr(exc, 'status_code', None) or getattr(exc, 'http_status', None)
    name = type(exc).__name__
    if status == 401:
        return '鉴权失败（HTTP 401）：API Key 无效，或与该 Base URL 不匹配'
    if status == 403:
        return '访问被拒绝（HTTP 403）：请确认该 Key 是否有此模型的权限'
    if status == 404:
        return '接口或模型不存在（HTTP 404）：请检查 Base URL 与模型名'
    if status == 429:
        return '调用被限流（HTTP 429）：请稍后重试'
    if status in (500, 502, 503, 504):
        return f'模型服务端错误（HTTP {status}）：请稍后重试'
    low = f'{name} {exc}'.lower()
    if 'timeout' in low or 'timed out' in low:
        return '调用超时：请检查网络，或 Base URL 是否可达'
    if ('connection' in low or 'connect' in low
            or 'getaddrinfo' in low or 'name or service' in low):
        return f'无法连接 {base}：请检查网络与 Base URL'
    return f'{name}：{_scrub(str(exc), key)}'[:300]


def _group(base: str, model: str, key: str | None, source: str) -> dict:
    """把 (base, model, key, 来源) 收成响应片段 —— key 只给掩码。"""
    return {
        'base_url': base or '',
        'model': model or '',
        'has_key': bool(key),
        'key_masked': llm_store.mask_key(key),
        'source': source,
    }


def _snapshot() -> ModelSettingsOut:
    """当前生效配置（文本组 + 视觉组），与抽取链路取到的是同一份。"""
    tb, tm, tk = llm_config.text_config()
    vb, vm, vk = llm_config.vision_config()
    text = _group(tb, tm, tk, llm_config.text_config_source())
    vision = _group(vb, vm, vk, llm_config.vision_config_source())
    return ModelSettingsOut(vision=ModelGroup(**vision), **text)


@router.get('/settings/model', response_model=ModelSettingsOut,
            summary='查看模型配置')
def get_model_settings() -> ModelSettingsOut:
    """返回当前生效的模型配置；key 只有掩码，绝不返回明文。"""
    return _snapshot()


@router.put('/settings/model', response_model=ModelSettingsOut,
            summary='保存模型配置',
            dependencies=[_WRITE_ROLES],
            responses={403: {'description': '权限不足：需要 admin / reviewer 角色'}})
def put_model_settings(body: ModelSettingsIn) -> ModelSettingsOut:
    """写入运行时设置并落盘，返回保存后的生效配置。

    只提交的字段会被改动（`exclude_unset`），空字符串表示清除该项。
    """
    patch = body.model_dump(exclude_unset=True)
    llm_store.update_overrides(patch)
    # 只记字段名，绝不记取值
    log.info('模型配置已更新：%s', ','.join(sorted(patch)) or '(无字段)')
    return _snapshot()


@router.post('/settings/model/test', response_model=ModelTestOut,
             summary='测试模型连通性',
             dependencies=[_WRITE_ROLES],
             responses={403: {'description': '权限不足：需要 admin / reviewer 角色'}})
def test_model_settings() -> ModelTestOut:
    """用当前生效配置做一次最小调用，返回 ok / 耗时 / 可读错误。"""
    base, model, key = llm_config.text_config()
    if not key:
        return ModelTestOut(
            ok=False, model=model, base_url=base, latency_ms=0,
            error='未配置 API Key：请在本页填入并保存，或设置环境变量 LLM_API_KEY')
    if not model:
        return ModelTestOut(ok=False, model='', base_url=base, latency_ms=0,
                            error='未配置模型名，请先填写模型名')

    t0 = time.perf_counter()
    try:
        from openai import OpenAI                      # noqa: PLC0415
        client = OpenAI(api_key=key, base_url=base, timeout=15.0, max_retries=0)
        resp = client.chat.completions.create(
            model=model,
            messages=[{'role': 'user', 'content': '只回复 ok'}],
            max_tokens=8,
        )
        latency = int((time.perf_counter() - t0) * 1000)
        log.info('模型连通性测试通过：model=%s latency_ms=%s', model, latency)
        return ModelTestOut(ok=True, model=model, base_url=base,
                            latency_ms=latency, error=None)
    except ImportError:
        return ModelTestOut(ok=False, model=model, base_url=base, latency_ms=0,
                            error='服务端缺少 openai 依赖，无法发起测试调用')
    except Exception as exc:                           # noqa: BLE001
        latency = int((time.perf_counter() - t0) * 1000)
        msg = _readable_error(exc, base, key)
        log.warning('模型连通性测试失败：model=%s latency_ms=%s %s',
                    model, latency, msg)               # msg 已脱敏
        return ModelTestOut(ok=False, model=model, base_url=base,
                            latency_ms=latency, error=msg)


@router.delete('/settings/model', response_model=ModelSettingsOut,
               summary='清除模型配置',
               dependencies=[_WRITE_ROLES],
               responses={403: {'description': '权限不足：需要 admin / reviewer 角色'}})
def delete_model_settings() -> ModelSettingsOut:
    """清除运行时配置，回落环境变量 / 默认值。"""
    llm_store.clear_overrides()
    log.info('模型配置已清除，回落环境变量')
    return _snapshot()
