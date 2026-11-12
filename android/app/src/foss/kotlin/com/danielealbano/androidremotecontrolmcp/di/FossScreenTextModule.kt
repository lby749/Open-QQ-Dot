package com.danielealbano.androidremotecontrolmcp.di

import com.danielealbano.androidremotecontrolmcp.services.screentext.DisabledScreenTextReader
import com.danielealbano.androidremotecontrolmcp.services.screentext.ScreenTextReader
import dagger.Binds
import dagger.Module
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

/**
 * foss flavor (F-Droid, no Google Play Services): there is no on-device recognizer to bind, so `phone_screen_text`
 * answers with the accessibility text alone and says the picture could not be read.
 */
@Module
@InstallIn(SingletonComponent::class)
abstract class FossScreenTextModule {
    @Binds
    @Singleton
    abstract fun bindScreenTextReader(impl: DisabledScreenTextReader): ScreenTextReader
}
