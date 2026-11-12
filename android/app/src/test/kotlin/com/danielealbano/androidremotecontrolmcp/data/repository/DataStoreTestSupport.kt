package com.danielealbano.androidremotecontrolmcp.data.repository

import org.junit.jupiter.api.Assumptions.assumeFalse

/**
 * androidx.datastore 1.2.1 fails to rename its temp file on the Windows JVM ("Unable to rename", FileStorage.kt).
 * Tests that write through DataStore are skipped there and still run on other platforms.
 */
internal fun assumeDataStoreWritesSupported() {
    assumeFalse(
        System.getProperty("os.name").startsWith("Windows"),
        "androidx.datastore cannot rename its temp file on the Windows JVM",
    )
}
