@file:Suppress("MatchingDeclarationName")

package com.danielealbano.androidremotecontrolmcp.mcp.tools

import android.app.AppOpsManager
import android.app.usage.UsageStatsManager
import android.content.Context
import android.os.Process
import android.view.accessibility.AccessibilityNodeInfo
import com.danielealbano.androidremotecontrolmcp.mcp.McpToolException
import com.danielealbano.androidremotecontrolmcp.services.accessibility.AppHistory
import com.danielealbano.androidremotecontrolmcp.services.accessibility.McpAccessibilityService
import com.danielealbano.androidremotecontrolmcp.services.accessibility.PhoneContextEvents
import com.danielealbano.androidremotecontrolmcp.services.screencapture.ScreenCaptureProvider
import com.danielealbano.androidremotecontrolmcp.services.screentext.ScreenTextReader
import com.danielealbano.androidremotecontrolmcp.services.screentext.ScreenTextResult
import io.modelcontextprotocol.kotlin.sdk.types.CallToolResult
import io.modelcontextprotocol.kotlin.sdk.types.ToolSchema
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import java.time.LocalDate
import java.time.ZoneId

/**
 * Friend house phone context, the way 爱语 does it: the computer's model asks when it needs to know
 * (front app, today's usage, the chosen apps' history, the words on the screen) instead of the phone streaming events.
 */
class PhoneContextHandler(
    private val context: Context,
    private val screenCaptureProvider: ScreenCaptureProvider,
    private val screenTextReader: ScreenTextReader,
) {
    fun now(): CallToolResult = McpToolUtils.untrustedTextResult(PhoneContextEvents.now(context).toString())

    fun usageToday(): CallToolResult {
        if (!usageAccessGranted()) {
            throw McpToolException.PermissionDenied("使用情况访问权限未开启：在手机小屋点「授权使用情况访问」")
        }
        val manager = context.getSystemService(UsageStatsManager::class.java)
        val start =
            LocalDate
                .now()
                .atStartOfDay(ZoneId.systemDefault())
                .toInstant()
                .toEpochMilli()
        val stats =
            manager
                .queryAndAggregateUsageStats(start, System.currentTimeMillis())
                .values
                .filter { it.totalTimeInForeground >= MIN_USAGE_MILLIS }
                .sortedByDescending { it.totalTimeInForeground }
                .take(MAX_USAGE_APPS)
        val result =
            buildJsonArray {
                stats.forEach { item ->
                    add(
                        buildJsonObject {
                            put("app", label(item.packageName))
                            put("package_name", item.packageName)
                            put("minutes", item.totalTimeInForeground / MILLIS_PER_MINUTE)
                        },
                    )
                }
            }
        return McpToolUtils.untrustedTextResult(result.toString())
    }

    fun history(arguments: JsonObject?): CallToolResult {
        val packageName = McpToolUtils.optionalString(arguments, "package_name", "")
        val hours = McpToolUtils.optionalInt(arguments, "hours", DEFAULT_HOURS)
        if (hours !in 1..MAX_HOURS) throw McpToolException.InvalidParams("hours must be 1..$MAX_HOURS")
        val chosen = AppHistory.apps(context)
        if (chosen.isEmpty()) return McpToolUtils.untrustedTextResult("no app chosen for history")
        val entries =
            buildJsonArray {
                AppHistory.read(context, packageName, hours, MAX_ENTRIES).forEach { item ->
                    add(
                        buildJsonObject {
                            put(
                                "time",
                                java.time.Instant
                                    .ofEpochMilli(item.optLong("t"))
                                    .toString(),
                            )
                            put("app", label(item.optString("pkg")))
                            put("text", item.optString("text"))
                        },
                    )
                }
            }
        return McpToolUtils.untrustedTextResult(
            buildJsonObject {
                putJsonArray("apps") { chosen.forEach { add(label(it)) } }
                put("entries", entries)
            }.toString(),
        )
    }

    fun setHistoryApps(arguments: JsonObject?): CallToolResult {
        val packages =
            (arguments?.get("packages") as? JsonArray)?.mapNotNull { it.jsonPrimitive.contentOrNull }
                ?: throw McpToolException.InvalidParams("packages must be an array of package names")
        val chosen = AppHistory.setApps(context, packages)
        return McpToolUtils.textResult("history apps: " + chosen.joinToString(","))
    }

    private fun usageAccessGranted(): Boolean {
        val ops = context.getSystemService(AppOpsManager::class.java)
        return ops.unsafeCheckOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), context.packageName) ==
            AppOpsManager.MODE_ALLOWED
    }

    /**
     * The words on the screen right now. The accessibility tree carries the text of ordinary app screens, so it is
     * read first; only when it is too thin does the phone read the same picture itself ([screenTextReader], on-device,
     * nothing uploaded). Private screens (lock screen, screen off, password or payment pages, blacklisted apps)
     * answer with no text at all, exactly as the other friend-house tools do.
     */
    suspend fun screenText(): CallToolResult {
        val service =
            McpAccessibilityService.instance
                ?: throw McpToolException.PermissionDenied("无障碍服务未开启：在手机小屋点「无障碍」并按系统提示授权")
        val root = service.rootInActiveWindow
        if (!service.phonePrivacyEnabled()) {
            return McpToolUtils.untrustedTextResult(privateScreenJson("privacy_mode_off"))
        }
        if (!PhoneContextEvents.contentAllowed(context, root)) {
            return McpToolUtils.untrustedTextResult(privateScreenJson("private_screen"))
        }
        val treeLines = screenLines(root)
        val treeText = treeLines.joinToString("\n")
        val wantsPicture = treeText.length < MIN_TREE_CHARS && screenCaptureProvider.isScreenCaptureAvailable()
        val picture = if (wantsPicture) readPicture() else null
        return McpToolUtils.untrustedTextResult(
            buildJsonObject {
                put("package_name", root?.packageName?.toString().orEmpty())
                put("foreground_app", label(root?.packageName?.toString().orEmpty()))
                put("text_source", if (picture == null) "screen" else "screen+picture")
                put("text", treeText.take(MAX_CHARS))
                putJsonArray("lines") { treeLines.take(MAX_LINES).forEach { add(it) } }
                if (picture != null) {
                    put("picture_status", picture.status)
                    put("picture_text", picture.text.take(MAX_CHARS))
                    putJsonArray("picture_lines") { picture.lines.take(MAX_LINES).forEach { add(it) } }
                }
            }.toString(),
        )
    }

    private class PictureText(
        val status: String,
        val text: String,
        val lines: List<String>,
    )

    /** Captures the screen, reads it on the device and recycles the bitmap; never keeps or returns the picture. */
    private suspend fun readPicture(): PictureText {
        val captured =
            screenCaptureProvider
                .captureScreenshotBitmap(maxWidth = MAX_PICTURE_WIDTH)
                .getOrElse { return PictureText("capture_failed", "", emptyList()) }
        return try {
            when (val result = screenTextReader.recognize(captured)) {
                is ScreenTextResult.Recognized -> {
                    val lines = cleanLines(result.text)
                    PictureText("recognized", lines.joinToString("\n"), lines)
                }

                ScreenTextResult.Disabled -> {
                    PictureText("reader_unavailable", "", emptyList())
                }

                is ScreenTextResult.Failed -> {
                    PictureText(result.reason, "", emptyList())
                }
            }
        } finally {
            captured.recycle()
        }
    }

    /** Visible text of the tree, skipping what the user is typing and anything a password field guards. */
    internal fun screenLines(root: AccessibilityNodeInfo?): List<String> {
        if (root == null) return emptyList()
        val lines = LinkedHashSet<String>()
        val queue = java.util.ArrayDeque<AccessibilityNodeInfo>()
        queue.add(root)
        var visited = 0
        while (queue.isNotEmpty() && lines.size < MAX_LINES && visited++ < MAX_NODES) {
            val node = queue.removeFirst()
            if (node.isEditable || node.isPassword) continue
            if (node.isVisibleToUser) {
                val value = (node.text ?: node.contentDescription)?.toString()?.trim().orEmpty()
                if (value.length >= MIN_LINE_CHARS) lines.add(value.replace(WHITESPACE, " "))
            }
            for (index in 0 until node.childCount) node.getChild(index)?.let(queue::add)
        }
        return lines.toList()
    }

    internal fun cleanLines(text: String): List<String> =
        LinkedHashSet(
            text
                .split('\n')
                .map { it.replace(WHITESPACE, " ").trim() }
                .filter { it.length >= MIN_LINE_CHARS },
        ).toList()

    private fun privateScreenJson(reason: String): String =
        buildJsonObject {
            put("text_source", "none")
            put("text", "")
            put("reason", reason)
        }.toString()

    private fun label(packageName: String): String =
        runCatching {
            context.packageManager
                .getApplicationLabel(context.packageManager.getApplicationInfo(packageName, 0))
                .toString()
        }.getOrDefault(packageName)

    fun register(
        registrar: LoggedToolRegistrar,
        toolNamePrefix: String,
    ) {
        val none = ToolSchema(properties = buildJsonObject { }, required = emptyList())
        registrar.addTool(
            toolName = NOW,
            name = "$toolNamePrefix$NOW",
            description = "The app currently in front on the user's phone, seconds spent in it, screen on/locked.",
            inputSchema = none,
        ) { now() }
        registrar.addTool(
            toolName = USAGE,
            name = "$toolNamePrefix$USAGE",
            description = "Minutes the user spent in each app today (top $MAX_USAGE_APPS).",
            inputSchema = none,
        ) { usageToday() }
        registrar.addTool(
            toolName = HISTORY,
            name = "$toolNamePrefix$HISTORY",
            description = "Text the user viewed recently in the apps chosen for history, newest first.",
            inputSchema =
                ToolSchema(
                    properties =
                        buildJsonObject {
                            putJsonObject("package_name") {
                                put("type", "string")
                                put("description", "One chosen app; empty = all chosen apps.")
                            }
                            putJsonObject("hours") {
                                put("type", "integer")
                                put("description", "Look back this many hours (1..$MAX_HOURS, default $DEFAULT_HOURS).")
                            }
                        },
                    required = emptyList(),
                ),
        ) { request -> history(request.arguments) }
        registrar.addTool(
            toolName = SET_APPS,
            name = "$toolNamePrefix$SET_APPS",
            description = "Choose at most two apps whose viewed text is kept on the phone; [] stops and clears it.",
            inputSchema =
                ToolSchema(
                    properties =
                        buildJsonObject {
                            putJsonObject("packages") {
                                put("type", "array")
                                putJsonObject("items") { put("type", "string") }
                            }
                        },
                    required = listOf("packages"),
                ),
        ) { request -> setHistoryApps(request.arguments) }
        registrar.addTool(
            toolName = SCREEN,
            name = "$toolNamePrefix$SCREEN",
            description =
                "The words on the user's screen right now: the visible text of the current app, plus words read " +
                    "on the phone from the same picture when the app shows little text. Private screens return none.",
            inputSchema = none,
        ) { screenText() }
    }

    companion object {
        const val NOW = "phone_now"
        const val USAGE = "phone_usage_today"
        const val HISTORY = "app_history"
        const val SET_APPS = "set_history_apps"
        const val SCREEN = "phone_screen_text"
        private const val MIN_USAGE_MILLIS = 60_000L
        private const val MILLIS_PER_MINUTE = 60_000L
        private const val MAX_USAGE_APPS = 15
        private const val DEFAULT_HOURS = 24
        private const val MAX_HOURS = 72
        private const val MAX_ENTRIES = 30
        private const val MIN_TREE_CHARS = 40
        private const val MIN_LINE_CHARS = 2
        private const val MAX_LINES = 40
        private const val MAX_NODES = 600
        private const val MAX_CHARS = 2_000
        private const val MAX_PICTURE_WIDTH = 1_280
        private val WHITESPACE = Regex("\\s+")
    }
}

fun registerPhoneContextTools(
    registrar: LoggedToolRegistrar,
    context: Context,
    screenCaptureProvider: ScreenCaptureProvider,
    screenTextReader: ScreenTextReader,
    toolNamePrefix: String,
) {
    PhoneContextHandler(context, screenCaptureProvider, screenTextReader).register(registrar, toolNamePrefix)
}
