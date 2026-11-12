package com.danielealbano.androidremotecontrolmcp.services.accessibility

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class AppHistoryTopicsTest {
    @Test
    fun `only caption topics are kept, never comments names or buttons`() {
        val screen =
            listOf(
                "关注",
                "未点赞，喜欢1.9万，按钮",
                "@玉小鱼",
                "傲娇真的没市场了吗… #lwl #救赎 >",
                "榊原恒一,这一天还是来了,3小时前, · 吉林,回复 按钮,",
                "简单的早餐 #一杯咖啡 #每日早... 展开",
                "给大家看一下日本的AI发展成什么样了 展开>",
                "傲娇真的没市场了吗… #lwl #救赎 >",
            ).joinToString("\n")
        assertEquals("#lwl #救赎\n#一杯咖啡\n~给大家看一下日本的AI发", AppHistory.topics(screen))
    }

    @Test
    fun `a screen without captions stores nothing`() {
        assertEquals("", AppHistory.topics("我的钱包\n保险权益：你有168元车主省钱卡待领取\n首页"))
    }
}
