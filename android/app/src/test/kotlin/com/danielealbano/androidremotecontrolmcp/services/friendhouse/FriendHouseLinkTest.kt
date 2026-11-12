package com.danielealbano.androidremotecontrolmcp.services.friendhouse

import android.content.Context
import android.content.SharedPreferences
import com.sun.net.httpserver.HttpServer
import io.mockk.every
import io.mockk.mockk
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.Timeout
import java.net.InetSocketAddress
import java.util.concurrent.atomic.AtomicReference

class FriendHouseLinkTest {
    private val token = "computer-token-0123456789"

    @Test
    fun `pairing URL must identify one friend and stay on the mesh`() {
        val pairing = FriendHouseLink.parse("http://100.64.0.1:8790/", token, "12345")!!
        assertEquals("12345", pairing.friendId)
        assertEquals("http://100.64.0.1:8790/", pairing.pcUrl)
        for (qq in listOf(null, "", "1234", "abc", "1234567890123")) {
            assertNull(FriendHouseLink.parse("http://100.64.0.1:8790/", token, qq))
        }
        for (host in listOf("192.168.1.1", "10.126.126.1", "100.63.255.255", "100.128.0.1", "100.64.0.999")) {
            assertNull(FriendHouseLink.parse("http://$host:8790/", token, "12345"))
        }
        assertEquals("12345", FriendHouseLink.parse("http://100.127.255.254:8790/", token, "12345")!!.friendId)
    }

    @Test
    fun `pairing URL may be the computer's Tailscale Funnel address`() {
        val pairing = FriendHouseLink.parse("https://lby.tailff4e90.ts.net/", token, "12345")!!
        assertEquals("https://lby.tailff4e90.ts.net/", pairing.pcUrl)
        for (url in listOf("http://lby.tailff4e90.ts.net/", "https://ts.net/", "https://evil.com/", "https://lby.ts.net.evil.com/")) {
            assertNull(FriendHouseLink.parse(url, token, "12345"), url)
        }
    }

    @Test
    fun `in-app scan reads the house QR or the pairing link`() {
        val expected = FriendHouseLink.Pairing("https://lby.tailff4e90.ts.net/", token, "12345")
        assertEquals(expected, FriendHouseLink.fromScan("https://lby.tailff4e90.ts.net/?qq=12345#t=$token"))
        assertEquals(
            expected,
            FriendHouseLink.fromScan("friendhouse://pair?pc=https%3A%2F%2Flby.tailff4e90.ts.net%2F&qq=12345&t=$token"),
        )
        assertEquals("http://100.64.0.1:8790/", FriendHouseLink.fromScan("http://100.64.0.1:8790/?qq=12345#t=$token")!!.pcUrl)
        for (text in listOf(
            "https://lby.tailff4e90.ts.net/?qq=12345",
            "https://evil.com/?qq=12345#t=$token",
            "friendhouse://other?pc=https%3A%2F%2Flby.tailff4e90.ts.net%2F&qq=12345&t=$token",
            "hello",
            "",
        )) {
            assertNull(FriendHouseLink.fromScan(text), text)
        }
    }

    @Test
    fun `saved pairing retains the selected friend`() {
        val values = mutableMapOf<String, String?>()
        val context = context(values)
        val pairing = FriendHouseLink.Pairing("http://100.64.0.1:8790/", token, "67890")
        FriendHouseLink.save(context, pairing, relay = true)
        assertEquals(pairing, FriendHouseLink.pairing(context))
        assertTrue(FriendHouseLink.usesRelay(context))
    }

    @Test
    fun `legacy pairing retains the restricted tool set but has no friend scope`() {
        val context = context(mutableMapOf("pc_url" to "http://100.64.0.1:8790/", "pc_token" to token))
        assertTrue(FriendHouseLink.isPaired(context))
        assertEquals("", FriendHouseLink.pairing(context)!!.friendId)
    }

    @Test
    @Timeout(10)
    fun `registration carries the selected friend and its computer token`() {
        val request = AtomicReference<String>()
        val authorization = AtomicReference<String>()
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/api/phone/register") { exchange ->
            request.set(exchange.requestBody.bufferedReader().use { it.readText() })
            authorization.set(exchange.requestHeaders.getFirst("Authorization"))
            exchange.sendResponseHeaders(200, -1)
            exchange.close()
        }
        server.start()
        try {
            val pairing = FriendHouseLink.Pairing("http://127.0.0.1:${server.address.port}/", token, "12345")
            FriendHouseLink.registerRelay(pairing, "phone-token-0123456789")
            assertEquals("Bearer $token", authorization.get())
            assertTrue(request.get().contains("\"qq\":\"12345\""))
            assertTrue(request.get().contains("\"transport\":\"relay\""))
            assertTrue(!request.get().contains("mcp_url"))
        } finally {
            server.stop(0)
        }
    }

    private fun context(values: MutableMap<String, String?>): Context {
        val context = mockk<Context>()
        val prefs = mockk<SharedPreferences>()
        val booleans = mutableMapOf<String, Boolean>()
        val editor = mockk<SharedPreferences.Editor>()
        every { context.getSharedPreferences("friend_house", Context.MODE_PRIVATE) } returns prefs
        every { prefs.getString(any(), any()) } answers { values[firstArg()] ?: secondArg() }
        every { prefs.getBoolean(any(), any()) } answers { booleans[firstArg()] ?: secondArg() }
        every { prefs.edit() } returns editor
        every { editor.putString(any(), any()) } answers {
            values[firstArg()] = secondArg()
            editor
        }
        every { editor.putBoolean(any(), any()) } answers {
            booleans[firstArg()] = secondArg()
            editor
        }
        every { editor.apply() } returns Unit
        return context
    }
}
