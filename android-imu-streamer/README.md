# 📱 Android IMU LSL Streamer App

This is a minimal, high-performance Android application that streams your smartphone's accelerometer and gyroscope data over WebSockets directly to the local LSL bridge.

---

## 🛠️ How to Build & Run

You have two choices to build the app: using **Android Studio** (recommended) or the **Command Line**.

### Method 1: Android Studio (Recommended)
1. Launch **Android Studio**.
2. Select **Open** and choose the `/home/john/git/muse-workshop/android-imu-streamer` folder.
3. Android Studio will automatically resolve dependencies and index the project.
4. Connect your Android phone to your PC via USB (ensure **Developer Options** and **USB Debugging** are enabled on your phone).
5. Click the green **Run** button (or press `Shift + F10`) to build and deploy it directly to your phone!

### Method 2: Command Line (Gradle)
If you have the Android SDK and JDK 17 installed, you can build the `.apk` file directly from your terminal:

1. Navigate to the project folder:
   ```bash
   cd /home/john/git/muse-workshop/android-imu-streamer
   ```
2. Initialize Gradle wrappers and compile the debug APK:
   ```bash
   ./gradlew assembleDebug
   ```
3. Locate the compiled APK file:
   The built APK will be saved at:
   `app/build/outputs/apk/debug/app-debug.apk`
4. Copy/Install this APK onto any student Android phone!

---

## 📲 How to Use the App
1. Make sure your phone is connected to the **same Wi-Fi network** as the computer running the dashboard.
2. Open the app on your phone.
3. In the input box, type the PC's WebSocket URL:
   `ws://192.168.1.XX:8001`
4. Tap **Connect**.
5. Once connected, the status will show **Connected** (green) and live sensor readouts will populate on your screen.
6. Open the **3D IMU Trajectory** tab in the dashboard, click **Connect to Stream**, and wave your phone around to see it trace!
