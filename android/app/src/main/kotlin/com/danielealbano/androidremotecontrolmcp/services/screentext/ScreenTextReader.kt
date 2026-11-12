package com.danielealbano.androidremotecontrolmcp.services.screentext

import android.graphics.Bitmap

/**
 * Friend house `phone_screen_text`: the words on the screen, read on the phone.
 *
 * The accessibility tree already carries the text of ordinary app screens, so the reader is only asked when that
 * text is too thin (an image, a video, a game, a canvas). Recognition happens entirely on the device: the
 * screenshot never leaves the phone, and a build without an on-device recognizer reports [ScreenTextResult.Disabled]
 * instead of failing the call ([DisabledScreenTextReader]).
 */
interface ScreenTextReader {
    suspend fun recognize(bitmap: Bitmap): ScreenTextResult
}

/** Outcome of one on-device recognition attempt. */
sealed interface ScreenTextResult {
    /** Text found in the picture; empty when the picture holds no recognizable words. */
    data class Recognized(
        val text: String,
    ) : ScreenTextResult

    /** This build has no on-device recognizer (the FOSS flavor ships none). */
    data object Disabled : ScreenTextResult

    /** The recognizer exists but this attempt failed; [reason] is a short machine-readable token. */
    data class Failed(
        val reason: String,
    ) : ScreenTextResult
}
