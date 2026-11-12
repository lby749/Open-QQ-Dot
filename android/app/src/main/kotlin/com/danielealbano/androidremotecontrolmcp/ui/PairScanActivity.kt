package com.danielealbano.androidremotecontrolmcp.ui

import android.os.Bundle
import com.journeyapps.barcodescanner.CaptureActivity
import com.journeyapps.barcodescanner.DecoratedBarcodeView
import com.journeyapps.barcodescanner.camera.CameraSettings

/**
 * 2026-10-12 user: the pairing scanner did not focus. The library's default AUTO mode re-triggers a one-shot focus
 * every 2 s, which many phones ignore; continuous focus (falls back to auto where unsupported) is set after the
 * library builds its settings from the intent in onCreate and before the camera opens in onResume.
 */
class PairScanActivity : CaptureActivity() {
    private lateinit var scanner: DecoratedBarcodeView

    override fun initializeContent(): DecoratedBarcodeView = super.initializeContent().also { scanner = it }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        scanner.barcodeView.cameraSettings =
            scanner.barcodeView.cameraSettings.apply { focusMode = CameraSettings.FocusMode.CONTINUOUS }
    }
}
