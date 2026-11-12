package com.danielealbano.androidremotecontrolmcp.ui.components

import com.danielealbano.androidremotecontrolmcp.data.model.ServerLogEntry

/** Localize the visible summary without rewriting the stored diagnostic evidence. */
fun diagnosticSummary(entry: ServerLogEntry): String {
    val message = entry.message.trim()
    if (message.firstOrNull()?.let { it in '\u3400'..'\u9fff' } == true) return message
    return when {
        message == "Bearer token changed" -> "手机访问令牌已更新"
        message == "OAuth enabled" -> "授权登录已启用"
        message == "OAuth disabled" -> "授权登录已关闭"
        message == "Server starting" -> "手机服务正在启动"
        message.startsWith("Server started on ") -> "手机服务已启动"
        message == "Server stopping" -> "手机服务正在停止"
        message == "Server stopped" -> "手机服务已停止"
        message.startsWith("Server error:") -> "手机服务出现异常"
        message.startsWith("Tunnel connecting") -> "正在建立跨网络连接"
        message.startsWith("Tunnel connected:") -> "跨网络连接已建立"
        message.startsWith("Tunnel error:") -> "跨网络连接出现异常"
        message == "Privacy mode ready (model)" -> "隐私保护模型已就绪"
        message == "Privacy mode ready (deterministic only)" -> "隐私保护规则已就绪"
        message.startsWith("Privacy mode UNAVAILABLE:") -> "隐私保护尚未就绪，数据读取工具已暂停"
        message == "Privacy mode disabled" -> "隐私保护已关闭"
        else -> {
            val subject = when (entry.type) {
                ServerLogEntry.Type.TOOL_CALL -> "工具调用"
                ServerLogEntry.Type.TUNNEL -> "跨网络连接"
                ServerLogEntry.Type.SERVER -> "手机服务"
                ServerLogEntry.Type.OAUTH -> "授权登录"
                ServerLogEntry.Type.AUTH -> "请求认证"
                ServerLogEntry.Type.CHANNEL -> "事件通道"
                ServerLogEntry.Type.SETTINGS -> "手机设置"
                ServerLogEntry.Type.PRIVACY -> "隐私保护"
            }
            val lower = message.lowercase()
            if (entry.type == ServerLogEntry.Type.AUTH || entry.type == ServerLogEntry.Type.TOOL_CALL ||
                listOf("failed", "error", "denied", "unavailable", "invalid", "blocked", "unauthorized", "rejected").any { it in lower }) {
                "${subject}出现异常，请查看技术详情"
            } else {
                "${subject}状态已更新"
            }
        }
    }
}
