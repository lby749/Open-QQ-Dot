package com.danielealbano.androidremotecontrolmcp.services.friendhouse

import android.content.Context
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.net.URI
import java.net.URL
import java.net.URLDecoder

/**
 * The computer's friend house this phone is paired with (scanned on the computer, opened from its phone page).
 * The phone reaches the computer through its Tailscale Funnel https address (no VPN on the phone) or the tailnet,
 * opens its page, and receives tool requests over an outbound connection without exposing the phone's MCP server.
 */
object FriendHouseLink {
    private const val PREFS = "friend_house"
    private const val PC_URL = "pc_url"
    private const val PC_TOKEN = "pc_token"
    private const val FRIEND_ID = "friend_id"
    private const val RELAY = "relay"

    data class Pairing(
        val pcUrl: String,
        val pcToken: String,
        val friendId: String = "",
    )

    fun pairing(context: Context): Pairing? {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val url = prefs.getString(PC_URL, null) ?: return null
        val token = prefs.getString(PC_TOKEN, null) ?: return null
        return Pairing(url, token, prefs.getString(FRIEND_ID, "").orEmpty())
    }

    /** In friend-house mode the phone's server offers only the read-only screen tool and the phone context tools. */
    fun isPaired(context: Context): Boolean = pairing(context) != null

    fun save(
        context: Context,
        pairing: Pairing,
        relay: Boolean = false,
    ) {
        context
            .getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(PC_URL, pairing.pcUrl)
            .putString(PC_TOKEN, pairing.pcToken)
            .putString(FRIEND_ID, pairing.friendId)
            .putBoolean(RELAY, relay)
            .apply()
    }

    /** `friendhouse://pair?pc=https://pc.tailnet.ts.net/&qq=12345&t=TOKEN` (or the tailnet http address); one friend's token. */
    fun parse(
        pc: String?,
        token: String?,
        friendId: String?,
    ): Pairing? {
        if (pc.isNullOrBlank() || token.isNullOrBlank() || !TOKEN.matches(token) || !QQ.matches(friendId.orEmpty())) return null
        val url = runCatching { URL(pc) }.getOrNull() ?: return null
        if (url.protocol == "https" && funnel(url.host)) {
            return Pairing("https://${url.host}${if (url.port > 0) ":" + url.port else ""}/", token, friendId.orEmpty())
        }
        if (url.protocol != "http" || !meshOrLocal(url.host)) return null
        return Pairing("http://${url.host}:${if (url.port > 0) url.port else url.defaultPort}/", token, friendId.orEmpty())
    }

    /**
     * 2026-10-12 user: what the in-app scanner read — the house QR on the computer's friend page
     * (`https://pc.tailnet.ts.net/?qq=12345#t=TOKEN`) or a `friendhouse://pair?...` link.
     */
    fun fromScan(text: String): Pairing? {
        val uri = runCatching { URI(text.trim()) }.getOrNull() ?: return null
        val query = params(uri.rawQuery)
        if (uri.scheme == "friendhouse") return if (uri.host == "pair") parse(query["pc"], query["t"], query["qq"]) else null
        return parse("${uri.scheme}://${uri.rawAuthority}/", params(uri.rawFragment)["t"], query["qq"])
    }

    private fun params(raw: String?): Map<String, String> =
        raw
            .orEmpty()
            .split('&')
            .mapNotNull { part ->
                val pair = part.split('=', limit = 2)
                if (pair.size == 2) URLDecoder.decode(pair[0], "UTF-8") to URLDecoder.decode(pair[1], "UTF-8") else null
            }.toMap()

    fun usesRelay(context: Context): Boolean = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(RELAY, false)

    /** Register an outbound connection; the phone has no externally reachable MCP address. */
    fun registerRelay(
        pairing: Pairing,
        phoneToken: String,
    ) {
        PhoneRelay.request(
            pairing.pcUrl + "api/phone/register",
            pairing.pcToken,
            phoneToken,
            buildJsonObject {
                put("qq", pairing.friendId)
                put("transport", "relay")
                put("token", phoneToken)
            },
        )
    }

    /** Tailscale IPv4 range; loopback stays allowed for a local test. */
    private fun meshOrLocal(host: String): Boolean {
        if (host == "127.0.0.1" || host == "localhost") return true
        val parts = host.split('.')
        if (parts.size != 4) return false
        val octets = parts.map { it.toIntOrNull() ?: return false }
        return octets.all { it in 0..255 } && octets[0] == 100 && octets[1] in 64..127
    }

    /** Tailscale Funnel names: `<machine>.<tailnet>.ts.net`. */
    private fun funnel(host: String): Boolean = FUNNEL.matches(host.lowercase())

    private val FUNNEL = Regex("([a-z0-9-]+\\.){2}ts\\.net")
    private val TOKEN = Regex("[A-Za-z0-9_-]{16,128}")
    private val QQ = Regex("[0-9]{5,12}")
}
