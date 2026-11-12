package com.danielealbano.androidremotecontrolmcp.services.accessibility

import android.app.KeyguardManager
import android.content.Context
import android.os.PowerManager
import android.view.accessibility.AccessibilityNodeInfo
import io.mockk.every
import io.mockk.mockk
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class PhoneContextEventsTest {
    @Test
    fun `ordinary input is ignored but focused input password and payment stay blocked`() {
        val context = mockk<Context>()
        val power = mockk<PowerManager>()
        val keyguard = mockk<KeyguardManager>()
        every { context.getSystemService(PowerManager::class.java) } returns power
        every { context.getSystemService(KeyguardManager::class.java) } returns keyguard
        every { power.isInteractive } returns true
        every { keyguard.isKeyguardLocked } returns false
        val input = mockk<AccessibilityNodeInfo>(relaxed = true)
        every { input.isEditable } returns true
        every { input.isFocused } returns false
        every { input.text } returns "不应读取的输入内容"
        val root = mockk<AccessibilityNodeInfo>(relaxed = true)
        every { root.packageName } returns "tv.danmaku.bili"
        every { root.isVisibleToUser } returns true
        every { root.contentDescription } returns null
        every { root.text } returns "三角洲行动视频详情"
        every { root.childCount } returns 1
        every { root.getChild(0) } returns input
        assertEquals("", PhoneContextEvents.contentBlockReason(context, root))
        every { root.packageName } returns "com.android.chrome"
        assertEquals("", PhoneContextEvents.contentBlockReason(context, root))
        every { root.packageName } returns "com.example.reader"
        assertEquals("", PhoneContextEvents.contentBlockReason(context, root))
        every { root.packageName } returns "com.tencent.mobileqq"
        assertEquals("app_not_allowed", PhoneContextEvents.contentBlockReason(context, root))
        every { root.packageName } returns "tv.danmaku.bili"
        every { input.isFocused } returns true
        assertEquals("editing", PhoneContextEvents.contentBlockReason(context, root))
        every { input.isFocused } returns false
        every { input.isPassword } returns true
        assertEquals("password", PhoneContextEvents.contentBlockReason(context, root))
        every { input.isPassword } returns false
        every { root.text } returns "私信"
        every { root.isClickable } returns true
        assertEquals("", PhoneContextEvents.contentBlockReason(context, root))
        every { root.isClickable } returns false
        assertEquals("sensitive_page", PhoneContextEvents.contentBlockReason(context, root))
        every { root.text } returns "支付页面"
        assertEquals("sensitive_page", PhoneContextEvents.contentBlockReason(context, root))
        every { root.text } returns "我的私信"
        assertEquals("sensitive_page", PhoneContextEvents.contentBlockReason(context, root))
        every { root.packageName } returns "com.example.bank"
        assertEquals("app_not_allowed", PhoneContextEvents.contentBlockReason(context, root))
    }
}
