package com.danielealbano.androidremotecontrolmcp.services.friendhouse

import com.danielealbano.androidremotecontrolmcp.data.model.ServerConfig
import com.sun.net.httpserver.HttpServer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.Timeout
import java.net.InetSocketAddress

class PhoneRelayTest {
    @Test
    @Timeout(10)
    fun `relay executes through authenticated local MCP and preserves privacy response`() {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        val methods = mutableListOf<String>()
        val sessions = mutableListOf<String?>()
        val privateResult = "{\"content\":[{\"type\":\"text\",\"text\":\"private_screen\"}]}"
        server.createContext("/mcp") { exchange ->
            assertEquals("Bearer local-phone-token", exchange.requestHeaders.getFirst("Authorization"))
            sessions.add(exchange.requestHeaders.getFirst("Mcp-Session-Id"))
            val body = Json.parseToJsonElement(exchange.requestBody.bufferedReader().use { it.readText() }).jsonObject
            val method = body.getValue("method").jsonPrimitive.content
            methods.add(method)
            val result =
                when (method) {
                    "initialize" -> {
                        "{}"
                    }

                    "tools/list" -> {
                        "{\"tools\":[{\"name\":\"android_phone_screen_text\"}]}"
                    }

                    "tools/call" -> {
                        val params = body.getValue("params").jsonObject
                        assertEquals("android_phone_screen_text", params["name"]!!.jsonPrimitive.content)
                        privateResult
                    }

                    else -> {
                        "{}"
                    }
                }
            if (method == "notifications/initialized") {
                exchange.sendResponseHeaders(202, -1)
                exchange.close()
            } else {
                exchange.responseHeaders.add("Mcp-Session-Id", "local-session")
                val bytes = "{\"jsonrpc\":\"2.0\",\"result\":$result}".toByteArray()
                exchange.sendResponseHeaders(200, bytes.size.toLong())
                exchange.responseBody.use { it.write(bytes) }
            }
        }
        server.start()
        try {
            val client = PhoneRelay.LocalMcp(ServerConfig(port = server.address.port, bearerToken = "local-phone-token"))
            val answer =
                client.call(
                    buildJsonObject {
                        put("name", "phone_screen_text")
                        put("arguments", JsonObject(emptyMap()))
                    },
                )
            assertEquals(Json.parseToJsonElement(privateResult), answer)
            assertEquals(listOf("initialize", "notifications/initialized", "tools/list", "tools/call"), methods)
            assertEquals(listOf(null, "local-session", "local-session", "local-session"), sessions)
            assertThrows(IllegalStateException::class.java) { client.call(buildJsonObject { put("name", "tap") }) }
            assertEquals(4, methods.size)
        } finally {
            server.stop(0)
        }
    }
}
