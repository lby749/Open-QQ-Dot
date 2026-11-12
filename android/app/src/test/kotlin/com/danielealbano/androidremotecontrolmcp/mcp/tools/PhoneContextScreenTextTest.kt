package com.danielealbano.androidremotecontrolmcp.mcp.tools

import android.content.Context
import android.graphics.Bitmap
import android.view.accessibility.AccessibilityNodeInfo
import com.danielealbano.androidremotecontrolmcp.services.screencapture.ScreenCaptureProvider
import com.danielealbano.androidremotecontrolmcp.services.screentext.ScreenTextReader
import com.danielealbano.androidremotecontrolmcp.services.screentext.ScreenTextResult
import io.mockk.every
import io.mockk.mockk
import kotlinx.coroutines.test.runTest
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class PhoneContextScreenTextTest {
    private val screenCaptureProvider = mockk<ScreenCaptureProvider>(relaxed = true)
    private val handler = PhoneContextHandler(mockk<Context>(relaxed = true), screenCaptureProvider, NoReader)

    /** A reader that finds nothing: the friend-house flow must still answer with what the tree gave it. */
    private object NoReader : ScreenTextReader {
        override suspend fun recognize(bitmap: Bitmap): ScreenTextResult = ScreenTextResult.Recognized("")
    }

    private fun node(
        text: String? = null,
        description: String? = null,
        visible: Boolean = true,
        editable: Boolean = false,
        password: Boolean = false,
    ) = mockk<AccessibilityNodeInfo>(relaxed = true).apply {
        every { this@apply.text } returns text
        every { contentDescription } returns description
        every { isVisibleToUser } returns visible
        every { isEditable } returns editable
        every { isPassword } returns password
        every { childCount } returns 0
    }

    @Test
    fun `tree text keeps visible words and skips typing, passwords, hidden and one-character nodes`() {
        val hidden = node(text = "屏幕外的文字", visible = false)
        val typing = node(text = "正在输入的草稿", editable = true)
        val password = node(password = true, description = "密码")
        val tiny = node(text = "·")
        val title = node(text = "三角洲行动  视频详情")
        val body = node(description = "评论区：这把枪后坐力大")
        val root =
            mockk<AccessibilityNodeInfo>(relaxed = true).apply {
                every { text } returns null
                every { contentDescription } returns null
                every { isVisibleToUser } returns true
                every { isEditable } returns false
                every { isPassword } returns false
                every { childCount } returns 6
                every { getChild(0) } returns hidden
                every { getChild(1) } returns typing
                every { getChild(2) } returns password
                every { getChild(3) } returns tiny
                every { getChild(4) } returns title
                every { getChild(5) } returns body
            }

        val lines = handler.screenLines(root)

        assertEquals(listOf("三角洲行动 视频详情", "评论区：这把枪后坐力大"), lines)
    }

    @Test
    fun `tree text is de-duplicated and a missing tree yields nothing`() {
        val repeated = node(text = "同样的标题")
        val root =
            mockk<AccessibilityNodeInfo>(relaxed = true).apply {
                every { text } returns null
                every { contentDescription } returns null
                every { isVisibleToUser } returns true
                every { isEditable } returns false
                every { isPassword } returns false
                every { childCount } returns 2
                every { getChild(0) } returns repeated
                every { getChild(1) } returns repeated
            }

        assertEquals(listOf("同样的标题"), handler.screenLines(root))
        assertEquals(emptyList<String>(), handler.screenLines(null))
    }

    @Test
    fun `picture text is trimmed to readable lines`() {
        val recognized = "  第一行  文字  \n\n  ·  \n第二行文字\n第一行 文字\n"

        assertEquals(listOf("第一行 文字", "第二行文字"), handler.cleanLines(recognized))
        assertEquals(emptyList<String>(), handler.cleanLines(""))
    }

    @Test
    fun `a build without an on-device reader reports it instead of failing`() =
        runTest {
            val reader =
                com.danielealbano.androidremotecontrolmcp.services.screentext
                    .DisabledScreenTextReader()

            val result = reader.recognize(mockk<Bitmap>(relaxed = true))

            assertTrue(result is ScreenTextResult.Disabled)
        }
}
