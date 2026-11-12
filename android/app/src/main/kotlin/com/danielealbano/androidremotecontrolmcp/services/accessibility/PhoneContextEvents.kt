package com.danielealbano.androidremotecontrolmcp.services.accessibility

import android.app.KeyguardManager
import android.content.Context
import android.os.BatteryManager
import android.os.PowerManager
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import com.danielealbano.androidremotecontrolmcp.data.model.ChannelEvent
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.time.Instant

/** Metadata only. No node text, notification text, identifiers or screenshot is emitted. */
object PhoneContextEvents {
    val events = MutableSharedFlow<ChannelEvent>(extraBufferCapacity = 32)
    private var currentPackage = ""
    private var startedAt = SystemClock.elapsedRealtime()
    private var lastAt = startedAt
    private var activeMillis = 0L
    private var wasInteractive = false
    private var lastBlockReason: String? = null
    private var lastSafetyCheckAt = 0L
    private val privateApps =
        setOf(
            "com.eg.android.AlipayGphone",
            "com.tencent.mm",
            "com.tencent.mobileqq",
            "com.android.mms",
            "com.google.android.apps.messaging",
            "com.android.systemui",
            "com.android.contacts",
            "com.android.dialer",
            "com.huawei.photos",
            "com.hihonor.photos",
            "com.google.android.apps.photos",
        )
    private val privateAppPattern =
        Regex(
            "bank|pay|wallet|auth|password|lastpass|1password|bitwarden|keepass|health|medical|hospital|gallery|private",
            RegexOption.IGNORE_CASE,
        )
    private val sensitiveText = Regex("密码|验证码|支付|余额|银行卡|身份证|地址|联系人|聊天|私信|私聊|password|otp|payment", RegexOption.IGNORE_CASE)
    private val navigationLabels = setOf("私信", "聊天", "联系人", "地址")

    fun contentAllowed(
        context: Context,
        node: AccessibilityNodeInfo?,
    ): Boolean = contentBlockReason(context, node).isEmpty()

    /** Friend house `phone_now`: the app in front and how long it has been in use; app names only, no content. */
    fun now(context: Context) =
        buildJsonObject {
            val power = context.getSystemService(PowerManager::class.java)
            val keyguard = context.getSystemService(KeyguardManager::class.java)
            val active = activeMillis + if (wasInteractive) SystemClock.elapsedRealtime() - lastAt else 0L
            put("package_name", currentPackage)
            put(
                "foreground_app",
                runCatching {
                    context.packageManager.getApplicationLabel(context.packageManager.getApplicationInfo(currentPackage, 0)).toString()
                }.getOrDefault(currentPackage),
            )
            put("seconds_in_app", active / 1000)
            put("screen_on", power.isInteractive)
            put("is_locked", keyguard.isKeyguardLocked)
        }

    internal fun contentBlockReason(
        context: Context,
        node: AccessibilityNodeInfo?,
    ): String {
        val power = context.getSystemService(PowerManager::class.java)
        val keyguard = context.getSystemService(KeyguardManager::class.java)
        if (!power.isInteractive) return "screen_off"
        if (keyguard.isKeyguardLocked) return "locked"
        if (node == null) return "ui_unavailable"
        val packageName = node.packageName?.toString().orEmpty()
        if (packageName.isBlank() || packageName in privateApps || privateAppPattern.containsMatchIn(packageName)) return "app_not_allowed"
        val queue = java.util.ArrayDeque<AccessibilityNodeInfo>()
        queue.add(node)
        var count = 0
        while (queue.isNotEmpty()) {
            val item = queue.removeFirst()
            if (++count > 500) return "tree_limit"
            if (item.isPassword) return "password"
            if (item.isEditable) {
                if (item.isFocused) return "editing"
                for (index in 0 until item.childCount) item.getChild(index)?.let(queue::add)
                continue // Never inspect ordinary input contents, even when unfocused.
            }
            val labels =
                listOf(
                    item.text
                        ?.toString()
                        .orEmpty()
                        .trim(),
                    item.contentDescription
                        ?.toString()
                        .orEmpty()
                        .trim(),
                )
            val navigationOnly =
                item.isClickable && labels.any { it in navigationLabels } &&
                    labels.all { it.isEmpty() || it in navigationLabels }
            if (
                item.isVisibleToUser && !navigationOnly && (
                    sensitiveText.containsMatchIn(item.text?.toString().orEmpty()) ||
                        sensitiveText.containsMatchIn(item.contentDescription?.toString().orEmpty())
                )
            ) {
                return "sensitive_page"
            }
            for (index in 0 until item.childCount) item.getChild(index)?.let(queue::add)
        }
        return ""
    }

    fun emit(
        context: Context,
        node: AccessibilityNodeInfo?,
        candidate: String?,
        safetyOnly: Boolean = false,
    ) {
        val now = SystemClock.elapsedRealtime()
        if (safetyOnly && now - lastSafetyCheckAt < 5_000) return
        lastSafetyCheckAt = now
        val packageName = node?.packageName?.toString() ?: candidate ?: currentPackage
        if (packageName.isBlank()) return
        val reason = contentBlockReason(context, node)
        if (safetyOnly && packageName == currentPackage && reason == lastBlockReason) return
        lastBlockReason = reason
        if (wasInteractive) activeMillis += now - lastAt
        lastAt = now
        val changed = packageName != currentPackage
        val previous = currentPackage
        val previousDuration = activeMillis / 1000
        if (changed) {
            currentPackage = packageName
            startedAt = now
            activeMillis = 0
        }
        val appName =
            runCatching {
                context.packageManager.getApplicationLabel(context.packageManager.getApplicationInfo(packageName, 0)).toString()
            }.getOrDefault(packageName)
        val power = context.getSystemService(PowerManager::class.java)
        val keyguard = context.getSystemService(KeyguardManager::class.java)
        val battery = context.getSystemService(BatteryManager::class.java)
        wasInteractive = power.isInteractive && !keyguard.isKeyguardLocked
        val event =
            ChannelEvent(
                type = if (changed) "foreground_app" else "device_state",
                timestamp = Instant.now().toString(),
                data =
                    buildJsonObject {
                        put("package_name", packageName)
                        put("foreground_app", appName)
                        put("foreground_duration", activeMillis / 1000)
                        if (changed && previous.isNotBlank()) {
                            put("previous_app", previous)
                            put("previous_duration", previousDuration)
                        }
                        put("screen_on", power.isInteractive)
                        put("is_locked", keyguard.isKeyguardLocked)
                        put("is_charging", battery.isCharging)
                        put("content_safe", reason.isEmpty())
                        put("content_block_reason", reason)
                    },
            )
        events.tryEmit(event)
    }
}
