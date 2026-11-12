package com.danielealbano.androidremotecontrolmcp.services.tunnel

import android.os.Build
import android.util.Log
import com.danielealbano.androidremotecontrolmcp.data.model.ServerConfig
import com.danielealbano.androidremotecontrolmcp.data.model.TunnelEndpoint
import com.danielealbano.androidremotecontrolmcp.data.model.TunnelProviderType
import com.danielealbano.androidremotecontrolmcp.data.model.TunnelStatus
import com.ngrok.Forwarder
import com.ngrok.Session
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.URL
import javax.inject.Inject

/**
 * ngrok tunnel provider.
 *
 * Uses the ngrok-java library (in-process, JNI-based) to create an
 * HTTPS tunnel. Requires an authtoken and optionally supports a
 * custom domain.
 *
 * Available on `arm64-v8a` and `x86_64` devices — graceful error on
 * unsupported ABIs.
 */
class NgrokTunnelProvider
    @Inject
    constructor() : TunnelProvider {
        private val _status = MutableStateFlow<TunnelStatus>(TunnelStatus.Disconnected)
        override val status: StateFlow<TunnelStatus> = _status.asStateFlow()

        private val mutex = Mutex()
        private var session: Session? = null
        private var forwarder: Forwarder.Endpoint? = null
        private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        private var monitorJob: Job? = null
        internal var endpointProbe: (String) -> Boolean = ::endpointAvailable

        override suspend fun start(
            localPort: Int,
            config: ServerConfig,
        ) {
            mutex.withLock {
                check(session == null) { "Tunnel is already running" }
                monitorJob?.cancel()
                monitorJob = null

                if (!isSupportedAbi()) {
                    val abis = Build.SUPPORTED_ABIS?.joinToString() ?: "unknown"
                    _status.value =
                        TunnelStatus.Error(
                            "ngrok is not supported on this device architecture " +
                                "($abis). Use Cloudflare instead.",
                        )
                    return
                }

                if (config.ngrokAuthtoken.isEmpty()) {
                    _status.value = TunnelStatus.Error("ngrok authtoken is required")
                    return
                }

                _status.value = TunnelStatus.Connecting

                try {
                    withContext(Dispatchers.IO) {
                        connect(localPort, config)
                    }
                } catch (
                    @Suppress("TooGenericExceptionCaught") e: Exception,
                ) {
                    Log.e(TAG, "Failed to start ngrok tunnel", e)
                    _status.value = TunnelStatus.Error("Failed to start ngrok: ${e.message}")
                    closeSession()
                }
                monitorJob =
                    scope.launch {
                        var failures = 0
                        while (isActive) {
                            delay(30_000)
                            if (checkAndRecover(localPort, config)) failures = 0 else failures++
                            if (failures >= 3) {
                                _status.value = TunnelStatus.Error("公网连接连续检查失败，请检查手机网络及 ngrok 账号后重新启动")
                                break
                            }
                        }
                    }
            }
        }

        private fun connect(
            localPort: Int,
            config: ServerConfig,
        ) {
            val sess = Session.withAuthtoken(config.ngrokAuthtoken).metadata("android-remote-control-mcp").connect()
            session = sess
            val builder = sess.httpEndpoint()
            if (config.ngrokDomain.isNotEmpty()) builder.domain(config.ngrokDomain)
            val fwd = builder.forward(URL("http://localhost:$localPort"))
            forwarder = fwd
            _status.value =
                if (endpointProbe(fwd.url)) {
                    TunnelStatus.Connected(
                        endpoints = listOf(TunnelEndpoint(url = fwd.url, valid = true)),
                        providerType = TunnelProviderType.NGROK,
                    )
                } else {
                    TunnelStatus.Error("代理会话已建立，但公网端点尚不可达，将自动重试")
                }
        }

        internal suspend fun checkAndRecover(
            localPort: Int,
            config: ServerConfig,
        ): Boolean =
            mutex.withLock {
                withContext(Dispatchers.IO) {
                    val url = forwarder?.url
                    if (url != null && endpointProbe(url)) return@withContext true
                    _status.value = TunnelStatus.Error("公网端点不可达，正在自动重连（不读取屏幕）")
                    closeSession()
                    try {
                        connect(localPort, config)
                        val connected = _status.value is TunnelStatus.Connected
                        if (!connected) _status.value = TunnelStatus.Error("重连后公网端点仍不可达，将稍后重试")
                        connected
                    } catch (e: Exception) {
                        closeSession()
                        _status.value = TunnelStatus.Error("跨网络重连失败，将稍后重试；请检查手机网络")
                        false
                    }
                }
            }

        private fun closeSession() {
            runCatching { forwarder?.close() }
            runCatching { session?.close() }
            cleanup()
        }

        override suspend fun stop() {
            monitorJob?.cancel()
            monitorJob = null
            mutex.withLock {
                if (session == null) {
                    _status.value = TunnelStatus.Disconnected
                    return
                }

                @Suppress("TooGenericExceptionCaught")
                try {
                    withContext(Dispatchers.IO) {
                        forwarder?.close()
                        session?.close()
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "Error closing ngrok session", e)
                } finally {
                    cleanup()
                    _status.value = TunnelStatus.Disconnected
                }
            }
        }

        private fun cleanup() {
            forwarder = null
            session = null
        }

        companion object {
            private const val TAG = "MCP:NgrokTunnel"
            private val SUPPORTED_ABIS = setOf("arm64-v8a", "x86_64")

            /** Pure ABI-membership check, separated from [Build] so it is unit-testable. */
            internal fun isAbiSupported(deviceAbis: Array<String>): Boolean = deviceAbis.any { it in SUPPORTED_ABIS }

            internal fun isSupportedAbi(): Boolean = isAbiSupported(Build.SUPPORTED_ABIS)

            private fun endpointAvailable(url: String): Boolean {
                var connection: HttpURLConnection? = null
                return try {
                    connection = URL(url.trimEnd('/') + "/mcp").openConnection() as HttpURLConnection
                    connection.connectTimeout = 5_000
                    connection.readTimeout = 5_000
                    connection.instanceFollowRedirects = false
                    connection.setRequestProperty("ngrok-skip-browser-warning", "true")
                    connection.responseCode in setOf(200, 202, 401, 405)
                } catch (e: Exception) {
                    false
                } finally {
                    connection?.disconnect()
                }
            }
        }
    }
