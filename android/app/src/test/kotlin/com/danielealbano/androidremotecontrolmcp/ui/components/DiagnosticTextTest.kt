package com.danielealbano.androidremotecontrolmcp.ui.components

import com.danielealbano.androidremotecontrolmcp.data.model.ServerLogEntry
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class DiagnosticTextTest {
    @Test
    fun `visible connection summaries are Chinese while raw evidence stays unchanged`() {
        val entry = ServerLogEntry(1, ServerLogEntry.Type.SERVER, "Server started on 127.0.0.1:8080")
        assertEquals("手机服务已启动", diagnosticSummary(entry))
        assertEquals("Server started on 127.0.0.1:8080", entry.message)
        assertEquals("手机访问令牌已更新", diagnosticSummary(entry.copy(type = ServerLogEntry.Type.SETTINGS, message = "Bearer token changed")))
    }

    @Test
    fun `unknown diagnostics retain useful Chinese category and failure state`() {
        val entry = ServerLogEntry(1, ServerLogEntry.Type.AUTH, "Unknown upstream authentication error: test-code")
        assertEquals("请求认证出现异常，请查看技术详情", diagnosticSummary(entry))
        assertTrue(entry.message.contains("test-code"))
        assertEquals("事件通道状态已更新", diagnosticSummary(entry.copy(type = ServerLogEntry.Type.CHANNEL, message = "New event channel state")))
    }

    @Test
    fun `existing Chinese diagnostics and privacy blocking state are preserved`() {
        val entry = ServerLogEntry(1, ServerLogEntry.Type.PRIVACY, "Privacy mode UNAVAILABLE: missing model")
        assertEquals("隐私保护尚未就绪，数据读取工具已暂停", diagnosticSummary(entry))
        assertEquals("服务已暂停", diagnosticSummary(entry.copy(message = "服务已暂停")))
    }
}
