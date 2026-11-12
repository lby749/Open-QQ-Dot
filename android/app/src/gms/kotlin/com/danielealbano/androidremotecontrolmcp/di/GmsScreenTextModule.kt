package com.danielealbano.androidremotecontrolmcp.di

import com.danielealbano.androidremotecontrolmcp.services.screentext.MlKitScreenTextReader
import com.danielealbano.androidremotecontrolmcp.services.screentext.ScreenTextReader
import dagger.Binds
import dagger.Module
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

/**
 * gms flavor: `phone_screen_text` reads pictures with the bundled on-device ML Kit recognizer. The foss flavor binds
 * the disabled reader instead (FossScreenTextModule), so it reports that it cannot read the picture rather than failing.
 */
@Module
@InstallIn(SingletonComponent::class)
abstract class GmsScreenTextModule {
    @Binds
    @Singleton
    abstract fun bindScreenTextReader(impl: MlKitScreenTextReader): ScreenTextReader
}
