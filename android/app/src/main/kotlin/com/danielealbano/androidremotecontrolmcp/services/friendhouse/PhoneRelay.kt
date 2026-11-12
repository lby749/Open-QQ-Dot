package com.danielealbano.androidremotecontrolmcp.services.friendhouse

import android.content.Context
import com.danielealbano.androidremotecontrolmcp.data.model.BindingAddress
import com.danielealbano.androidremotecontrolmcp.data.model.ServerConfig
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.isActive
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/** Only outbound connections reach the PC. Existing MCP tools execute on the phone's loopback server. */
object PhoneRelay {
    private val tools = setOf("phone_now", "phone_usage_today", "phone_screen_text", "app_history", "set_history_apps")
    private const val TIMEOUT_MILLIS = 15_000
    private const val MAX_BYTES = 256 * 1024
    private const val RETRY_MILLIS = 3_000L

    suspend fun run(
        context: Context,
        config: ServerConfig,
    ) {
        val pairing = FriendHouseLink.pairing(context) ?: return
        if (pairing.friendId.isBlank() || config.bindingAddress != BindingAddress.LOCALHOST || config.httpsEnabled) return
        while (currentCoroutineContext().isActive && FriendHouseLink.pairing(context) == pairing) {
            try {
                val response = request(pairing.pcUrl + "api/phone/poll", pairing.pcToken, config.bearerToken, JsonObject(emptyMap()))
                val task = response["task"]?.jsonObject ?: continue
                if (task.isEmpty()) continue
                currentCoroutineContext().ensureActive()
                if (FriendHouseLink.pairing(context) != pairing) return
                val answer =
                    try {
                        LocalMcp(config).call(task)
                    } catch (e: IOException) {
                        errorResult(e.message ?: "手机工具执行失败")
                    } catch (e: IllegalStateException) {
                        errorResult(e.message ?: "手机工具执行失败")
                    }
                currentCoroutineContext().ensureActive()
                request(
                    pairing.pcUrl + "api/phone/result",
                    pairing.pcToken,
                    config.bearerToken,
                    buildJsonObject {
                        put("id", task.getValue("id"))
                        put("result", answer)
                    },
                )
            } catch (_: IOException) {
                delay(RETRY_MILLIS)
            } catch (_: IllegalStateException) {
                delay(RETRY_MILLIS)
            } catch (_: IllegalArgumentException) {
                delay(RETRY_MILLIS)
            }
        }
    }

    internal fun request(
        url: String,
        token: String,
        phoneToken: String,
        body: JsonObject,
    ): JsonObject = exchange(url, token, phoneToken, body, "").first

    private fun exchange(
        url: String,
        token: String,
        phoneToken: String,
        body: JsonObject,
        session: String,
    ): Pair<JsonObject, String?> {
        val connection = URL(url).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = "POST"
            connection.instanceFollowRedirects = false
            connection.connectTimeout = TIMEOUT_MILLIS
            connection.readTimeout = TIMEOUT_MILLIS
            connection.doOutput = true
            connection.setRequestProperty("Authorization", "Bearer $token")
            connection.setRequestProperty("Content-Type", "application/json")
            connection.setRequestProperty("Accept", "application/json, text/event-stream")
            if (phoneToken.isNotEmpty()) connection.setRequestProperty("X-Phone-Token", phoneToken)
            if (session.isNotEmpty()) connection.setRequestProperty("Mcp-Session-Id", session)
            connection.outputStream.use { it.write(body.toString().toByteArray()) }
            check(connection.responseCode in 200..299) { "连接服务返回 ${connection.responseCode}" }
            val bytes = connection.inputStream.use { it.readNBytes(MAX_BYTES + 1) }
            check(bytes.size <= MAX_BYTES) { "手机结果过大" }
            val result = if (bytes.isEmpty()) JsonObject(emptyMap()) else Json.parseToJsonElement(bytes.decodeToString()).jsonObject
            return result to connection.getHeaderField("Mcp-Session-Id")
        } finally {
            connection.disconnect()
        }
    }

    private fun errorResult(message: String): JsonObject =
        buildJsonObject {
            put("isError", true)
            put(
                "content",
                JsonArray(
                    listOf(
                        buildJsonObject {
                            put("type", "text")
                            put("text", message.take(300))
                        },
                    ),
                ),
            )
        }

    internal class LocalMcp(
        private val config: ServerConfig,
    ) {
        private var session = ""
        private var sequence = 0

        private fun rpc(
            method: String,
            params: JsonObject,
            notification: Boolean = false,
        ): JsonObject {
            val body =
                buildJsonObject {
                    put("jsonrpc", "2.0")
                    if (!notification) put("id", ++sequence)
                    put("method", method)
                    put("params", params)
                }
            val (answer, newSession) = exchange("http://127.0.0.1:${config.port}/mcp", config.bearerToken, "", body, session)
            if (newSession != null) session = newSession
            check(answer["error"] == null) { "手机工具请求失败" }
            return answer["result"]?.jsonObject ?: JsonObject(emptyMap())
        }

        fun call(task: JsonObject): JsonObject {
            val name = task.getValue("name").jsonPrimitive.content
            check(name in tools) { "不允许的手机工具" }
            rpc(
                "initialize",
                buildJsonObject {
                    put("protocolVersion", "2025-03-26")
                    put("capabilities", JsonObject(emptyMap()))
                    put(
                        "clientInfo",
                        buildJsonObject {
                            put("name", "friend-house-relay")
                            put("version", "1")
                        },
                    )
                },
            )
            rpc("notifications/initialized", JsonObject(emptyMap()), notification = true)
            val listed = rpc("tools/list", JsonObject(emptyMap()))["tools"] as? JsonArray ?: error("手机没有工具列表")
            val names = listed.map { tool -> tool.jsonObject["name"]!!.jsonPrimitive.content }
            val matches = names.filter { it == name || it.endsWith("_$name") }
            check(matches.size == 1) { "手机上没有唯一的工具 $name" }
            return rpc(
                "tools/call",
                buildJsonObject {
                    put("name", matches.single())
                    put("arguments", task["arguments"] ?: JsonObject(emptyMap()))
                },
            )
        }
    }
}
