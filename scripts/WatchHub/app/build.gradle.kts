import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.example.multistreamwrap"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.example.multistreamwrap"
        minSdk = 21
        targetSdk = 36
        versionCode = 450
        versionName = "1.5.00"
    }

    signingConfigs {
        create("release") {
            storeFile = file("release.jks")
            storePassword = "multistream123"
            keyAlias = "multistream"
            keyPassword = "multistream123"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            signingConfig = signingConfigs.getByName("release")
        }
        getByName("debug") {
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }


    kotlin {
        compilerOptions {
            jvmTarget.set(JvmTarget.JVM_17)
        }
    }
}

dependencies {
    // Pure Android framework - koi external dependency nahi
}
