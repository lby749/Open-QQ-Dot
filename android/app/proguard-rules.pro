# R8 rules for the slimmed release build.
# The app uses JNI (onnxruntime, ML Kit), kotlinx.serialization, Ktor and Hilt, which all
# rely on reflection or native entry points that shrinking must keep.

-keepattributes *Annotation*, InnerClasses, Signature, EnclosingMethod

# Native entry points
-keepclasseswithmembernames class * {
    native <methods>;
}
-keep class ai.onnxruntime.** { *; }
-keep class com.google.mlkit.** { *; }
-dontwarn com.google.mlkit.**

# kotlinx.serialization
-keepclassmembers class **$$serializer { *; }
-keepclasseswithmembers class * {
    kotlinx.serialization.KSerializer serializer(...);
}
-keepclassmembers @kotlinx.serialization.Serializable class * {
    *** Companion;
}

# Ktor / Netty
-keep class io.ktor.** { *; }
-dontwarn io.ktor.**
-keep class io.netty.** { *; }
-dontwarn io.netty.**

# Hilt / Dagger
-keep class dagger.hilt.** { *; }
-keep class * extends dagger.hilt.internal.GeneratedComponent { *; }
-dontwarn dagger.hilt.**

# Bouncy Castle (self-signed TLS)
-keep class org.bouncycastle.** { *; }
-dontwarn org.bouncycastle.**

# Data models crossed via serialization
-keep class com.danielealbano.androidremotecontrolmcp.data.model.** { *; }
