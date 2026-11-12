package com.danielealbano.androidremotecontrolmcp.services.accessibility

import android.content.Context
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors

/**
 * Friend house: what the user viewed in the one or two apps chosen on the computer (`set_history_apps`).
 * Only the #topics of the captions on a settled screen ([topics]) are kept on the phone for [KEEP_MILLIS], never the
 * screen's text; nothing is recorded for other apps,
 * for private screens ([PhoneContextEvents.contentBlockReason]) or while no app is chosen.
 */
object AppHistory {
    private const val PREFS = "friend_house"
    private const val APPS_KEY = "history_apps"
    private const val FILE = "app_history_topics.jsonl"
    private const val LEGACY_FILE = "app_history.jsonl"
    private const val START_CHARS = 12
    private val CAPTION_END = Regex("(展开>?|>)$")
    private val TRUNCATED = Regex("\\S*(\\.\\.\\.|…)")
    private val TAG = Regex("#([^\\s#>…]+)")

    @Volatile private var legacyChecked = false
    private const val MAX_APPS = 2
    private const val MIN_GAP_MILLIS = 5_000L
    private const val KEEP_MILLIS = 3L * 24 * 60 * 60 * 1000
    private const val MAX_LINES = 40
    private const val MAX_CHARS = 1_200
    private const val MAX_ENTRIES = 3_000
    private const val MAX_NODES = 600

    private val writer = Executors.newSingleThreadExecutor()
    private val lastAt = mutableMapOf<String, Long>()
    private val lastText = mutableMapOf<String, String>()

    fun apps(context: Context): List<String> =
        context
            .getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(APPS_KEY, "")
            .orEmpty()
            .split(',')
            .filter { it.isNotBlank() }

    /** At most [MAX_APPS] package names; an empty list stops recording and clears what was kept. */
    fun setApps(
        context: Context,
        packages: List<String>,
    ): List<String> {
        val chosen =
            packages
                .map { it.trim() }
                .filter { PACKAGE.matches(it) }
                .distinct()
                .take(MAX_APPS)
        context
            .getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(APPS_KEY, chosen.joinToString(","))
            .apply()
        dropLegacy(context)
        writer.execute { prune(context, keep = chosen.toSet()) }
        return chosen
    }

    fun record(
        context: Context,
        root: AccessibilityNodeInfo?,
        packageName: String?,
    ) {
        dropLegacy(context)
        if (root == null || packageName.isNullOrBlank() || packageName !in apps(context)) return
        val now = SystemClock.elapsedRealtime()
        if (now - (lastAt[packageName] ?: 0L) < MIN_GAP_MILLIS) return
        lastAt[packageName] = now
        if (PhoneContextEvents.contentBlockReason(context, root).isNotEmpty()) return
        // 2026-10-13 user: only the #topics of what was watched are kept on the phone, never the screen's text
        val text = topics(visibleText(root))
        if (text.isBlank() || text == lastText[packageName]) return
        lastText[packageName] = text
        val line =
            JSONObject()
                .put("t", System.currentTimeMillis())
                .put("pkg", packageName)
                .put("text", text)
                .toString()
        writer.execute { File(context.filesDir, FILE).appendText(line + "\n") }
    }

    /** Newest first, entries of [packageName] (or every chosen app) from the last [hours]. */
    fun read(
        context: Context,
        packageName: String?,
        hours: Int,
        limit: Int,
    ): List<JSONObject> {
        dropLegacy(context)
        val file = File(context.filesDir, FILE)
        if (!file.exists()) return emptyList()
        val since = System.currentTimeMillis() - hours * 60L * 60 * 1000
        val chosen = apps(context).toSet()
        return file
            .readLines()
            .mapNotNull { runCatching { JSONObject(it) }.getOrNull() }
            .filter { it.optLong("t") >= since && it.optString("pkg") in chosen }
            .filter { packageName.isNullOrBlank() || it.optString("pkg") == packageName }
            .asReversed()
            .take(limit)
    }

    private fun prune(
        context: Context,
        keep: Set<String>,
    ) {
        val file = File(context.filesDir, FILE)
        if (!file.exists()) return
        val since = System.currentTimeMillis() - KEEP_MILLIS
        val lines =
            file.readLines().filter { line ->
                val item = runCatching { JSONObject(line) }.getOrNull()
                item != null && item.optLong("t") >= since && item.optString("pkg") in keep
            }
        file.writeText(lines.takeLast(MAX_ENTRIES).joinToString("") { it + "\n" })
    }

    /**
     * The video captions on a screen (a line ending with the app's "展开" / ">" control) reduced to their #topics, one
     * caption per line; a caption without a #topic keeps only its first [START_CHARS] characters, prefixed with "~".
     * Comments, names, buttons and counts are dropped. A word the app cut short ("..." / "…") is dropped too.
     */
    internal fun topics(text: String): String =
        text
            .lines()
            .map { it.trim() }
            .filter { CAPTION_END.containsMatchIn(it) }
            .distinct()
            .mapNotNull { line ->
                val cut = TRUNCATED.replace(line, "")
                val tags =
                    TAG
                        .findAll(cut)
                        .map { "#" + it.groupValues[1] }
                        .distinct()
                        .toList()
                if (tags.isNotEmpty()) {
                    tags.joinToString(" ")
                } else {
                    CAPTION_END
                        .replace(cut, "")
                        .trim(' ', '.', '…')
                        .take(START_CHARS)
                        .takeIf { it.isNotEmpty() }
                        ?.let { "~$it" }
                }
            }.joinToString("\n")

    /** Versions before 2026-10-13 kept the screens' raw text here; it is deleted, not converted. */
    private fun dropLegacy(context: Context) {
        if (!legacyChecked) {
            legacyChecked = true
            File(context.filesDir, LEGACY_FILE).delete()
        }
    }

    internal fun visibleText(root: AccessibilityNodeInfo): String {
        val lines = LinkedHashSet<String>()
        val queue = ArrayDeque<AccessibilityNodeInfo>()
        queue.add(root)
        var count = 0
        while (queue.isNotEmpty() && lines.size < MAX_LINES && count++ < MAX_NODES) {
            val node = queue.removeFirst()
            if (node.isEditable || node.isPassword) continue // never what the user is typing
            if (node.isVisibleToUser) {
                val value = (node.text ?: node.contentDescription)?.toString()?.trim().orEmpty()
                if (value.length >= 2) lines.add(value.replace(WHITESPACE, " "))
            }
            for (index in 0 until node.childCount) node.getChild(index)?.let(queue::add)
        }
        return lines.joinToString("\n").take(MAX_CHARS)
    }

    private val PACKAGE = Regex("[A-Za-z][\\w]*(\\.[A-Za-z][\\w]*)+")
    private val WHITESPACE = Regex("\\s+")
}
