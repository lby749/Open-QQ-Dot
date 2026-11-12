@file:Suppress("FunctionNaming", "LongMethod")

package com.danielealbano.androidremotecontrolmcp.ui.screens.settings

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.danielealbano.androidremotecontrolmcp.R
import com.danielealbano.androidremotecontrolmcp.data.model.OptionalToolPermission
import com.danielealbano.androidremotecontrolmcp.data.model.OptionalToolPermissions
import com.danielealbano.androidremotecontrolmcp.data.model.ServerStatus
import com.danielealbano.androidremotecontrolmcp.data.model.ToolPermissionsConfig
import com.danielealbano.androidremotecontrolmcp.ui.theme.WarningAmber
import com.danielealbano.androidremotecontrolmcp.ui.viewmodels.MainViewModel

private data class ParamEntry(
    val paramName: String,
    val displayName: String,
)

private data class ToolEntry(
    val toolName: String,
    val displayName: String,
    val params: List<ParamEntry> = emptyList(),
)

private data class ToolCategory(
    val header: String,
    val tools: List<ToolEntry>,
)

private val ALL_TOOL_CATEGORIES: List<ToolCategory> =
    listOf(
        ToolCategory(
            "界面读取",
            listOf(
                ToolEntry(
                    "get_screen_state",
                    "读取当前界面（get_screen_state）",
                    listOf(ParamEntry("include_screenshot", "同时读取截图（建议关闭）")),
                ),
            ),
        ),
        ToolCategory(
            "系统操作",
            listOf(
                ToolEntry("press_back", "按返回键"),
                ToolEntry("press_home", "返回桌面"),
                ToolEntry("press_recents", "打开最近任务"),
                ToolEntry("open_notifications", "打开通知栏"),
                ToolEntry("open_quick_settings", "打开快捷设置"),
                ToolEntry("dismiss_keyboard", "收起键盘"),
            ),
        ),
        ToolCategory(
            "触摸操作",
            listOf(
                ToolEntry("tap", "点击"),
                ToolEntry("long_press", "长按"),
                ToolEntry("double_tap", "双击"),
                ToolEntry("swipe", "滑动"),
                ToolEntry("scroll", "滚动"),
            ),
        ),
        ToolCategory(
            "手势",
            listOf(
                ToolEntry("pinch", "双指缩放"),
                ToolEntry("custom_gesture", "自定义手势"),
            ),
        ),
        ToolCategory(
            "界面元素操作",
            listOf(
                ToolEntry("find_nodes", "查找元素"),
                ToolEntry("click_node", "点击元素"),
                ToolEntry("long_click_node", "长按元素"),
                ToolEntry("tap_node", "触摸元素"),
                ToolEntry("scroll_to_node", "滚动到元素"),
            ),
        ),
        ToolCategory(
            "文字输入",
            listOf(
                ToolEntry("type_append_text", "追加文字"),
                ToolEntry("type_insert_text", "插入文字"),
                ToolEntry("type_replace_text", "替换文字"),
                ToolEntry("type_clear_text", "清空文字"),
                ToolEntry("press_key", "按键"),
            ),
        ),
        ToolCategory(
            "辅助工具",
            listOf(
                ToolEntry("get_clipboard", "读取剪贴板"),
                ToolEntry("set_clipboard", "写入剪贴板"),
                ToolEntry("wait_for_node", "等待元素出现"),
                ToolEntry("wait_for_idle", "等待界面空闲"),
                ToolEntry("get_node_details", "读取元素详情"),
            ),
        ),
        ToolCategory(
            "文件操作",
            listOf(
                ToolEntry("list_storage_locations", "列出存储位置"),
                ToolEntry("list_files", "列出文件"),
                ToolEntry("read_file", "读取文件"),
                ToolEntry("write_file", "写入文件"),
                ToolEntry("append_file", "追加文件内容"),
                ToolEntry("file_replace", "替换文件内容"),
                ToolEntry("download_from_url", "从网址下载"),
                ToolEntry("delete_file", "删除文件"),
            ),
        ),
        ToolCategory(
            "应用管理",
            listOf(
                ToolEntry("open_app", "打开应用"),
                ToolEntry("list_apps", "列出应用"),
                ToolEntry("close_app", "关闭应用"),
            ),
        ),
        ToolCategory(
            "摄像头",
            listOf(
                ToolEntry("list_cameras", "列出摄像头"),
                ToolEntry("list_camera_photo_resolutions", "列出拍照分辨率"),
                ToolEntry("list_camera_video_resolutions", "列出录像分辨率"),
                ToolEntry("take_camera_photo", "拍照"),
                ToolEntry("save_camera_photo", "保存照片"),
                ToolEntry(
                    "save_camera_video",
                    "保存视频",
                    listOf(ParamEntry("audio", "同时录音")),
                ),
            ),
        ),
        ToolCategory(
            "系统请求",
            listOf(
                ToolEntry("send_intent", "发送系统请求"),
                ToolEntry("open_uri", "打开链接"),
            ),
        ),
        ToolCategory(
            "通知操作",
            listOf(
                ToolEntry("notification_list", "列出通知"),
                ToolEntry("notification_open", "打开通知"),
                ToolEntry("notification_dismiss", "关闭通知"),
                ToolEntry("notification_snooze", "延后通知"),
                ToolEntry("notification_action", "执行通知操作"),
                ToolEntry("notification_reply", "回复通知"),
            ),
        ),
        ToolCategory(
            "定位",
            listOf(
                ToolEntry(
                    "get_location",
                    "读取位置",
                    listOf(ParamEntry("fresh_fix", "允许实时 GPS 定位")),
                ),
            ),
        ),
        ToolCategory(
            "分享",
            listOf(
                ToolEntry("get_shared_content", "读取分享内容"),
                ToolEntry("share_file_via_web", "通过网页分享文件"),
            ),
        ),
    )

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun McpToolsSettingsScreen(
    onBack: () -> Unit,
    onNavigateToPermissions: () -> Unit,
    modifier: Modifier = Modifier,
    viewModel: MainViewModel = hiltViewModel(),
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val serverStatus by viewModel.serverStatus.collectAsStateWithLifecycle()
    val perms by viewModel.toolPermissionsConfig.collectAsStateWithLifecycle()
    val cameraGranted by viewModel.isCameraPermissionGranted.collectAsStateWithLifecycle()
    val locationGranted by viewModel.isLocationPermissionGranted.collectAsStateWithLifecycle()
    val notificationListenerGranted by viewModel.isNotificationListenerEnabled.collectAsStateWithLifecycle()
    val microphoneGranted by viewModel.isMicrophonePermissionGranted.collectAsStateWithLifecycle()
    val controlsEnabled = serverStatus !is ServerStatus.Running && serverStatus !is ServerStatus.Starting

    // Refresh permissions on ON_RESUME — SAME pattern as PermissionsSettingsScreen.
    DisposableEffect(lifecycleOwner) {
        val observer =
            LifecycleEventObserver { _, event ->
                if (event == Lifecycle.Event.ON_RESUME) {
                    viewModel.refreshPermissionStatus(context)
                }
            }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    val isGranted: (OptionalToolPermission) -> Boolean = { permission ->
        isPermissionGranted(
            permission = permission,
            camera = cameraGranted,
            location = locationGranted,
            notificationListener = notificationListenerGranted,
            microphone = microphoneGranted,
        )
    }

    Column(modifier = modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text(stringResource(R.string.settings_mcp_tools_title)) },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = null)
                }
            },
            windowInsets = WindowInsets(0),
        )
        LazyColumn(modifier = Modifier.weight(1f)) {
            item {
                Text(
                    text = "修改后需停止并重新启动手机内容服务",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                )
            }
            ALL_TOOL_CATEGORIES.forEach { category ->
                val categoryPermissions =
                    category.tools.mapNotNull { OptionalToolPermissions.permissionForTool(it.toolName) }.distinct()
                item(key = "header_${category.header}") {
                    ToolCategoryHeader(
                        header = category.header,
                        missingPermission = categoryPermissions.any { !isGranted(it) },
                        onNavigateToPermissions = onNavigateToPermissions,
                    )
                }
                items(category.tools, key = { it.toolName }) { tool ->
                    ToolRow(
                        tool = tool,
                        perms = perms,
                        controlsEnabled = controlsEnabled,
                        categoryGranted = categoryPermissions.all { isGranted(it) },
                        isGranted = isGranted,
                        onNavigateToPermissions = onNavigateToPermissions,
                        onToolToggle = viewModel::updateToolEnabled,
                        onParamToggle = viewModel::updateParamEnabled,
                    )
                }
            }
        }
    }
}

/** Resolves whether an optional permission is granted from the individual permission flags. */
private fun isPermissionGranted(
    permission: OptionalToolPermission,
    camera: Boolean,
    location: Boolean,
    notificationListener: Boolean,
    microphone: Boolean,
): Boolean =
    when (permission) {
        OptionalToolPermission.CAMERA -> camera
        OptionalToolPermission.LOCATION -> location
        OptionalToolPermission.NOTIFICATION_LISTENER -> notificationListener
        OptionalToolPermission.MICROPHONE -> microphone
    }

@Composable
private fun ToolCategoryHeader(
    header: String,
    missingPermission: Boolean,
    onNavigateToPermissions: () -> Unit,
) {
    // Match the warning triangle to the header text size (respects font scaling).
    val warningIconSize =
        with(LocalDensity.current) {
            MaterialTheme.typography.titleMedium.fontSize
                .toDp()
        }
    if (!missingPermission) {
        Text(
            text = header,
            style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.primary,
            modifier = Modifier.padding(start = 16.dp, end = 16.dp, top = 16.dp, bottom = 4.dp),
        )
        return
    }
    Column(modifier = Modifier.clickable { onNavigateToPermissions() }) {
        Row(
            modifier = Modifier.padding(start = 16.dp, top = 16.dp, bottom = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = header,
                style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier.padding(end = 8.dp),
            )
            Icon(
                imageVector = Icons.Default.Warning,
                contentDescription = null,
                tint = WarningAmber,
                modifier = Modifier.size(warningIconSize),
            )
        }
        Text(
            text = stringResource(R.string.settings_mcp_tools_missing_permission),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(start = 16.dp, end = 16.dp, bottom = 4.dp),
        )
    }
}

@Composable
private fun ToolRow(
    tool: ToolEntry,
    perms: ToolPermissionsConfig,
    controlsEnabled: Boolean,
    categoryGranted: Boolean,
    isGranted: (OptionalToolPermission) -> Boolean,
    onNavigateToPermissions: () -> Unit,
    onToolToggle: (String, Boolean) -> Unit,
    onParamToggle: (String, String, Boolean) -> Unit,
) {
    val toolEnabled = perms.isToolEnabled(tool.toolName)
    ListItem(
        headlineContent = { Text(tool.displayName) },
        trailingContent = {
            Switch(
                checked = toolEnabled,
                onCheckedChange = { onToolToggle(tool.toolName, it) },
                enabled = controlsEnabled && categoryGranted,
            )
        },
    )
    if (toolEnabled) {
        tool.params.forEach { param ->
            ToolParamRow(
                toolName = tool.toolName,
                param = param,
                perms = perms,
                controlsEnabled = controlsEnabled,
                categoryGranted = categoryGranted,
                isGranted = isGranted,
                onNavigateToPermissions = onNavigateToPermissions,
                onParamToggle = onParamToggle,
            )
        }
    }
}

@Composable
private fun ToolParamRow(
    toolName: String,
    param: ParamEntry,
    perms: ToolPermissionsConfig,
    controlsEnabled: Boolean,
    categoryGranted: Boolean,
    isGranted: (OptionalToolPermission) -> Boolean,
    onNavigateToPermissions: () -> Unit,
    onParamToggle: (String, String, Boolean) -> Unit,
) {
    val paramPermission = OptionalToolPermissions.permissionForParam(toolName, param.paramName)
    val paramGranted = paramPermission == null || isGranted(paramPermission)
    val showParamNote = categoryGranted && !paramGranted
    val leadingIcon: (@Composable () -> Unit)? =
        if (showParamNote) {
            { Icon(imageVector = Icons.Default.Warning, contentDescription = null, tint = WarningAmber) }
        } else {
            null
        }
    val supportingNote: (@Composable () -> Unit)? =
        if (showParamNote) {
            {
                Text(
                    text = stringResource(R.string.settings_mcp_tools_param_missing_permission),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        } else {
            null
        }
    ListItem(
        headlineContent = { Text(param.displayName) },
        modifier =
            if (showParamNote) {
                Modifier.padding(start = 32.dp).clickable { onNavigateToPermissions() }
            } else {
                Modifier.padding(start = 32.dp)
            },
        leadingContent = leadingIcon,
        supportingContent = supportingNote,
        trailingContent = {
            Switch(
                checked = perms.isParamEnabled(toolName, param.paramName),
                onCheckedChange = { onParamToggle(toolName, param.paramName, it) },
                enabled = controlsEnabled && categoryGranted && paramGranted,
            )
        },
    )
}
