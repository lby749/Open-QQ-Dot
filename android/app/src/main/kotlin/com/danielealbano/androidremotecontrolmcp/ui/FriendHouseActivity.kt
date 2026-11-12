package com.danielealbano.androidremotecontrolmcp.ui

import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.QrCodeScanner
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.lifecycleScope
import com.danielealbano.androidremotecontrolmcp.data.model.BindingAddress
import com.danielealbano.androidremotecontrolmcp.data.model.ServerStatus
import com.danielealbano.androidremotecontrolmcp.data.repository.SettingsRepository
import com.danielealbano.androidremotecontrolmcp.services.friendhouse.FriendHouseLink
import com.danielealbano.androidremotecontrolmcp.services.mcp.McpServerService
import com.danielealbano.androidremotecontrolmcp.ui.components.CalloutCard
import com.danielealbano.androidremotecontrolmcp.ui.theme.AndroidRemoteControlMcpTheme
import com.google.zxing.BinaryBitmap
import com.google.zxing.MultiFormatReader
import com.google.zxing.RGBLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import dagger.hilt.android.AndroidEntryPoint
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import java.util.UUID
import javax.inject.Inject

/**
 * Friend house on the phone: the computer's house page (models and data stay on the computer) plus the pairing
 * that lets the computer's model ask this phone what is in front, today's usage, the chosen apps' history.
 * Opened from the launcher or from `friendhouse://pair?...` (the button on the computer's phone page).
 * 2026-10-12 user: pairing scans the computer's QR inside the app ("扫码配对"), connects and shows the house here
 * instead of the camera opening the page in the browser.
 *
 * 2026-10-12 user: drawn in the app's own Material 3 look (top bar, overflow menu, callout card); the page inside
 * takes the app's current colour scheme and hides its own header.
 */
@AndroidEntryPoint
class FriendHouseActivity : ComponentActivity() {
    @Inject lateinit var settingsRepository: SettingsRepository

    private lateinit var web: WebView
    private var houseName by mutableStateOf("QQ 直连")
    private var subtitle by mutableStateOf("")
    private var notice by mutableStateOf<String?>(null)
    private var paired by mutableStateOf(false)
    private var pageFailure by mutableStateOf(false)
    private var themeScript = ""
    private val scan = registerForActivityResult(ScanContract()) { result -> result.contents?.let(::scanned) }
    private val pickQr = registerForActivityResult(ActivityResultContracts.GetContent()) { uri -> uri?.let(::importQr) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        web =
            WebView(this).apply {
                settings.javaScriptEnabled = true
                settings.domStorageEnabled = true
                webViewClient = HouseClient()
                webChromeClient =
                    object : WebChromeClient() {
                        override fun onReceivedTitle(
                            view: WebView,
                            pageTitle: String?,
                        ) {
                            if (!pageFailure && !pageTitle.isNullOrBlank() && !pageTitle.startsWith("http")) houseName = pageTitle
                        }
                    }
            }
        setContent { AndroidRemoteControlMcpTheme { HouseScreen() } }
        onBackPressedDispatcher.addCallback(
            this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    if (web.canGoBack()) web.goBack() else finish()
                }
            },
        )
        handle(intent)
    }

    @OptIn(ExperimentalMaterial3Api::class)
    @Composable
    private fun HouseScreen() {
        val scheme = MaterialTheme.colorScheme
        val script = remember(scheme) { pageTheme(scheme) }
        SideEffect {
            themeScript = script
            web.setBackgroundColor(scheme.surface.toArgb())
            web.evaluateJavascript(script, null)
        }
        var menu by remember { mutableStateOf(false) }
        Scaffold(
            topBar = {
                TopAppBar(
                    title = {
                        Column {
                            Text(houseName, maxLines = 1, overflow = TextOverflow.Ellipsis)
                            if (subtitle.isNotEmpty()) {
                                Text(
                                    subtitle,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                )
                            }
                        }
                    },
                    actions = {
                        IconButton(onClick = ::startScan) {
                            Icon(Icons.Filled.QrCodeScanner, contentDescription = "扫码配对")
                        }
                        IconButton(onClick = {
                            if (pageFailure) retryPage() else web.evaluateJavascript("window.houseRefresh && houseRefresh()", null)
                        }) {
                            Icon(Icons.Filled.Refresh, contentDescription = "刷新")
                        }
                        Box {
                            IconButton(onClick = { menu = true }) {
                                Icon(Icons.Filled.MoreVert, contentDescription = "更多")
                            }
                            DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                                DropdownMenuItem(text = { Text("从图片扫码") }, onClick = {
                                    menu = false
                                    pickQrImage()
                                })
                                DropdownMenuItem(text = { Text("无障碍权限") }, onClick = {
                                    menu = false
                                    startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
                                })
                                DropdownMenuItem(text = { Text("使用情况访问") }, onClick = {
                                    menu = false
                                    startActivity(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS))
                                })
                                DropdownMenuItem(text = { Text("手机端设置") }, onClick = {
                                    menu = false
                                    startActivity(Intent(this@FriendHouseActivity, MainActivity::class.java))
                                })
                            }
                        }
                    },
                )
            },
        ) { padding ->
            Column(Modifier.padding(padding).fillMaxSize()) {
                notice?.let {
                    CalloutCard(
                        icon = Icons.Filled.Info,
                        title = it,
                        modifier = Modifier.padding(16.dp, 8.dp),
                        actions = if (it == PAIRING) null else if (pageFailure) {
                            { Button(onClick = ::retryPage) { Text("重新连接") } }
                        } else ({ Row { Button(onClick = ::startScan) { Text("扫码配对") }; Button(onClick = ::pickQrImage) { Text("从图片选") } } }),
                    )
                }
                if (paired && !pageFailure) {
                    AndroidView(factory = { web }, modifier = Modifier.fillMaxWidth().weight(1f))
                } else if (!paired) {
                    Column(Modifier.padding(24.dp)) {
                        Text("还没有配对", style = MaterialTheme.typography.headlineSmall)
                        Text(
                            "在电脑端打开对应好友的「QQ 直连」页，点手机配对二维码；" +
                                "然后用相机扫描，或从相册选择二维码截图。",
                            modifier = Modifier.padding(top = 10.dp, bottom = 18.dp),
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        Button(onClick = ::startScan) { Text("扫码配对") }
                        Button(onClick = ::pickQrImage, modifier = Modifier.padding(top = 8.dp)) {
                            Text("从相册选择二维码")
                        }
                    }
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handle(intent)
    }

    private fun handle(intent: Intent?) {
        if (intent?.action == "com.danielealbano.androidremotecontrolmcp.SCAN_PAIR") {
            startScan()
            return
        }
        val data = intent?.data
        if (data?.scheme == "friendhouse" && data.host == "pair") {
            val pairing =
                FriendHouseLink.parse(data.getQueryParameter("pc"), data.getQueryParameter("t"), data.getQueryParameter("qq"))
            if (pairing == null) notice = "配对链接无效，请点「扫码配对」重新扫电脑上的二维码。" else pair(pairing)
        } else {
            show()
        }
    }

    private fun show() {
        pageFailure = false
        val pairing = FriendHouseLink.pairing(this)
        if (pairing == null) {
            paired = false
            subtitle = ""
            return
        }
        paired = true
        if (pairing.friendId.isBlank()) {
            notice = "旧的共用配对已停用，请点「扫码配对」，重新扫对应好友「小屋状态」页的二维码。"
            web.loadUrl("about:blank")
            return
        }
        notice = null
        subtitle = "已配对 · 只有这位好友的模型能查询这台手机"
        if (FriendHouseLink.usesRelay(this)) {
            startForegroundService(Intent(this, McpServerService::class.java).apply { action = McpServerService.ACTION_START })
        }
        web.loadUrl(pairing.pcUrl + "?qq=" + pairing.friendId + "&app=1#t=" + pairing.pcToken)
    }

    private fun retryPage() {
        pageFailure = false
        notice = null
        web.reload()
    }

    private fun pickQrImage() {
        pickQr.launch("image/*")
    }

    private fun importQr(uri: Uri) {
        lifecycleScope.launch(Dispatchers.IO) {
            val text = runCatching {
                val bitmap = contentResolver.openInputStream(uri)?.use(BitmapFactory::decodeStream)
                    ?: return@runCatching null
                val pixels = IntArray(bitmap.width * bitmap.height)
                bitmap.getPixels(pixels, 0, bitmap.width, 0, 0, bitmap.width, bitmap.height)
                val source = RGBLuminanceSource(bitmap.width, bitmap.height, pixels)
                MultiFormatReader().decode(BinaryBitmap(HybridBinarizer(source))).text
            }.getOrNull()
            withContext(Dispatchers.Main) {
                if (text == null) {
                    notice = "图片里没有识别到二维码，请换一张清晰的配对截图。"
                } else {
                    scanned(text)
                }
            }
        }
    }

    private fun startScan() {
        scan.launch(
            ScanOptions()
                .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                .setPrompt("扫描电脑上好友「小屋状态」页的二维码")
                .setBeepEnabled(false)
                .setOrientationLocked(false)
                .setCaptureActivity(PairScanActivity::class.java),
        )
    }

    private fun scanned(text: String) {
        val pairing = FriendHouseLink.fromScan(text)
        if (pairing == null) notice = "这不是 QQ 直连的配对二维码，请扫电脑上对应好友「QQ 直连」页的二维码。" else pair(pairing)
    }

    private fun pair(pairing: FriendHouseLink.Pairing) {
        if (notice == PAIRING) return
        notice = PAIRING
        paired = true
        lifecycleScope.launch {
            try {
                val token = UUID.randomUUID().toString()
                withContext(Dispatchers.IO) { FriendHouseLink.registerRelay(pairing, token) }
                settingsRepository.updateTunnelEnabled(false)
                settingsRepository.updateOauthEnabled(false)
                settingsRepository.updateHttpsEnabled(false)
                settingsRepository.updateBindingAddress(BindingAddress.LOCALHOST)
                settingsRepository.updateBearerTokenEnabled(true)
                settingsRepository.updateBearerToken(token)
                FriendHouseLink.save(this@FriendHouseActivity, pairing, relay = true)
                restartServer() // the friend-house tool set and the new token take effect
                show()
            } catch (e: IllegalStateException) {
                notice = "配对失败：" + e.message
            } catch (e: java.io.IOException) {
                notice = "连不上电脑小屋：" + e.message + "。确认电脑小屋开着，电脑上 tailscale funnel 已开启。"
            }
        }
    }

    private suspend fun restartServer() {
        if (McpServerService.instance != null) {
            startService(Intent(this, McpServerService::class.java).apply { action = McpServerService.ACTION_STOP })
            withTimeout(SERVICE_TIMEOUT_MILLIS) {
                McpServerService.serverStatus.first { it is ServerStatus.Stopped && McpServerService.instance == null }
            }
        }
        startForegroundService(Intent(this, McpServerService::class.java).apply { action = McpServerService.ACTION_START })
        val state =
            withTimeout(SERVICE_TIMEOUT_MILLIS) {
                McpServerService.serverStatus.first { it is ServerStatus.Running || it is ServerStatus.Error }
            }
        check(state is ServerStatus.Running) { "手机服务启动失败，请在手机端设置查看" }
    }

    /** Only the paired computer's pages open inside the app; anything else goes to the browser. */
    private inner class HouseClient : WebViewClient() {
        override fun shouldOverrideUrlLoading(
            view: WebView,
            request: WebResourceRequest,
        ): Boolean {
            val pairing = FriendHouseLink.pairing(this@FriendHouseActivity) ?: return true
            val origin = Uri.parse(pairing.pcUrl)
            if (request.url.scheme == origin.scheme && request.url.host == origin.host && request.url.port == origin.port) return false
            if (request.url.scheme == "friendhouse") {
                handle(Intent(Intent.ACTION_VIEW, request.url))
                return true
            }
            startActivity(Intent(Intent.ACTION_VIEW, request.url))
            return true
        }

        override fun onPageFinished(
            view: WebView,
            url: String?,
        ) {
            view.evaluateJavascript(themeScript, null)
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (!request.isForMainFrame) return
            pageFailure = true
            houseName = "连接暂时中断"
            notice = "暂时无法连接电脑，配对已保留。电脑恢复连接后，点击重新连接。"
        }

        override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
            if (!request.isForMainFrame) return
            pageFailure = true
            houseName = "访问暂时不可用"
            notice = if (response.statusCode == 401 || response.statusCode == 403) {
                "电脑拒绝了当前配对的访问，请在电脑端检查这位好友的配对权限。"
            } else {
                "电脑服务暂时不可用，请稍后重新连接。"
            }
        }
    }

    companion object {
        private const val SERVICE_TIMEOUT_MILLIS = 15_000L
        private const val PAIRING = "正在配对…"

        /** The page's CSS colour variables (house_web.html) set from the app's current Material 3 scheme. */
        private fun pageTheme(scheme: ColorScheme): String {
            fun hex(color: Color) = String.format("#%06X", color.toArgb() and 0xFFFFFF)
            val colors =
                mapOf(
                    "--primary" to scheme.primary,
                    "--on-primary" to scheme.onPrimary,
                    "--primary-container" to scheme.primaryContainer,
                    "--on-primary-container" to scheme.onPrimaryContainer,
                    "--secondary-container" to scheme.secondaryContainer,
                    "--on-secondary-container" to scheme.onSecondaryContainer,
                    "--surface" to scheme.surface,
                    "--surface-low" to scheme.surfaceContainerLow,
                    "--surface-container" to scheme.surfaceContainer,
                    "--surface-high" to scheme.surfaceContainerHigh,
                    "--on-surface" to scheme.onSurface,
                    "--on-surface-variant" to scheme.onSurfaceVariant,
                    "--outline" to scheme.outline,
                    "--outline-variant" to scheme.outlineVariant,
                    "--error" to scheme.error,
                    "--inverse-surface" to scheme.inverseSurface,
                    "--inverse-on-surface" to scheme.inverseOnSurface,
                )
            return "(function(){var s=document.documentElement.style;" +
                colors.entries.joinToString("") { (name, color) -> "s.setProperty('$name','${hex(color)}');" } + "})()"
        }
    }
}
