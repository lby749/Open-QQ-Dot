package com.danielealbano.androidremotecontrolmcp.services.screentext

import android.graphics.Bitmap
import javax.inject.Inject

/**
 * Default [ScreenTextReader] for builds without an on-device recognizer: the FOSS flavor (F-Droid, no Google Play
 * Services) and the plain JVM unit tests. `phone_screen_text` then answers with the accessibility text alone.
 */
class DisabledScreenTextReader
    @Inject
    constructor() : ScreenTextReader {
        override suspend fun recognize(bitmap: Bitmap): ScreenTextResult = ScreenTextResult.Disabled
    }
