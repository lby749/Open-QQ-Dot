package com.danielealbano.androidremotecontrolmcp.services.screentext

import android.graphics.Bitmap
import android.util.Log
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions
import kotlinx.coroutines.suspendCancellableCoroutine
import javax.inject.Inject
import javax.inject.Singleton
import kotlin.coroutines.resume

/**
 * gms flavor: on-device text recognition (Google ML Kit, Chinese model, which also covers Latin script).
 *
 * The model is bundled in the APK, so recognition works offline and the picture is never uploaded. The recognizer
 * is created once and reused; ML Kit closes it when the process ends.
 */
@Singleton
class MlKitScreenTextReader
    @Inject
    constructor() : ScreenTextReader {
        private val recognizer by lazy { TextRecognition.getClient(ChineseTextRecognizerOptions.Builder().build()) }

        override suspend fun recognize(bitmap: Bitmap): ScreenTextResult {
            val image = InputImage.fromBitmap(bitmap, ROTATION_DEGREES)
            return try {
                suspendCancellableCoroutine { continuation ->
                    recognizer
                        .process(image)
                        .addOnSuccessListener { text ->
                            if (continuation.isActive) continuation.resume(ScreenTextResult.Recognized(text.text))
                        }.addOnFailureListener { error ->
                            Log.w(TAG, "on-device text recognition failed", error)
                            if (continuation.isActive) continuation.resume(ScreenTextResult.Failed("recognition_failed"))
                        }.addOnCanceledListener {
                            if (continuation.isActive) continuation.resume(ScreenTextResult.Failed("recognition_cancelled"))
                        }
                }
            } catch (error: IllegalStateException) {
                Log.w(TAG, "on-device text recognition unavailable", error)
                ScreenTextResult.Failed("recognizer_unavailable")
            }
        }

        private companion object {
            const val TAG = "MCP:ScreenTextReader"
            const val ROTATION_DEGREES = 0
        }
    }
