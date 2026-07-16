package com.neurotech.imustreamer

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.Locale
import android.view.WindowManager
import android.speech.SpeechRecognizer
import android.speech.RecognizerIntent
import android.speech.RecognitionListener
import android.content.Intent
import android.content.pm.PackageManager
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import android.widget.CheckBox

class MainActivity : AppCompatActivity(), SensorEventListener {

    private lateinit var sensorManager: SensorManager
    private var accelerometer: Sensor? = null
    private var gyroscope: Sensor? = null

    private lateinit var urlInput: EditText
    private lateinit var connectBtn: Button
    private lateinit var statusText: TextView
    private lateinit var accX: TextView
    private lateinit var accY: TextView
    private lateinit var accZ: TextView
    private lateinit var gyroX: TextView
    private lateinit var gyroY: TextView
    private lateinit var gyroZ: TextView
    private lateinit var voiceToggle: CheckBox
    private lateinit var voiceStatus: TextView
    private var speechRecognizer: SpeechRecognizer? = null
    private val REQUEST_RECORD_AUDIO_PERMISSION = 200

    private val client = OkHttpClient()
    private var webSocket: WebSocket? = null
    private var isConnected = false

    private val latestAcc = FloatArray(3) { 0f }
    private val latestGyro = FloatArray(3) { 0f }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        // Keep screen awake while this app is in the foreground
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        // Initialize UI Elements
        urlInput = findViewById(R.id.urlInput)
        connectBtn = findViewById(R.id.connectBtn)
        statusText = findViewById(R.id.statusText)
        accX = findViewById(R.id.accX)
        accY = findViewById(R.id.accY)
        accZ = findViewById(R.id.accZ)
        gyroX = findViewById(R.id.gyroX)
        gyroY = findViewById(R.id.gyroY)
        gyroZ = findViewById(R.id.gyroZ)
        voiceToggle = findViewById(R.id.voiceToggle)
        voiceStatus = findViewById(R.id.voiceStatus)

        voiceToggle.setOnCheckedChangeListener { _, isChecked ->
            if (isChecked) {
                startSpeechRecognition()
            } else {
                stopSpeechRecognition()
            }
        }

        // Initialize Sensors
        sensorManager = getSystemService(Context.SENSOR_SERVICE) as SensorManager
        accelerometer = sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
        gyroscope = sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)

        // Load saved URL from preferences (pre-filled default)
        val prefs = getPreferences(Context.MODE_PRIVATE)
        val savedUrl = prefs.getString("ws_url", "ws://192.168.1.72:8001")
        urlInput.setText(savedUrl)

        connectBtn.setOnClickListener {
            if (isConnected) {
                disconnect()
            } else {
                connect()
            }
        }
    }

    private fun connect() {
        val url = urlInput.text.toString().trim()
        if (url.isEmpty()) {
            Toast.makeText(this, "Please enter a valid URL", Toast.LENGTH_SHORT).show()
            return
        }

        // Save URL for next launches
        getPreferences(Context.MODE_PRIVATE).edit().putString("ws_url", url).apply()

        connectBtn.isEnabled = false
        statusText.text = "Status: Connecting..."

        val request = Request.Builder().url(url).build()
        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                runOnUiThread {
                    isConnected = true
                    connectBtn.isEnabled = true
                    connectBtn.text = "Disconnect"
                    connectBtn.setBackgroundColor(resources.getColor(android.R.color.holo_red_dark, theme))
                    statusText.text = "Status: Connected"
                    
                    // Register sensor listeners at high frequency (GAME rate)
                    accelerometer?.let {
                        sensorManager.registerListener(this@MainActivity, it, SensorManager.SENSOR_DELAY_GAME)
                    }
                    gyroscope?.let {
                        sensorManager.registerListener(this@MainActivity, it, SensorManager.SENSOR_DELAY_GAME)
                    }
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                runOnUiThread {
                    isConnected = false
                    connectBtn.isEnabled = true
                    connectBtn.text = "Connect"
                    connectBtn.setBackgroundColor(resources.getColor(android.R.color.holo_blue_dark, theme))
                    statusText.text = "Status: Connection Failed"
                    Toast.makeText(this@MainActivity, "Error: ${t.message}", Toast.LENGTH_LONG).show()
                    sensorManager.unregisterListener(this@MainActivity)
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                runOnUiThread {
                    isConnected = false
                    connectBtn.isEnabled = true
                    connectBtn.text = "Connect"
                    connectBtn.setBackgroundColor(resources.getColor(android.R.color.holo_blue_dark, theme))
                    statusText.text = "Status: Disconnected"
                    sensorManager.unregisterListener(this@MainActivity)
                }
            }
        })
    }

    private fun disconnect() {
        webSocket?.close(1000, "Goodbye")
        webSocket = null
        runOnUiThread {
            isConnected = false
            connectBtn.isEnabled = true
            connectBtn.text = "Connect"
            connectBtn.setBackgroundColor(resources.getColor(android.R.color.holo_blue_dark, theme))
            statusText.text = "Status: Disconnected"
            sensorManager.unregisterListener(this@MainActivity)
            stopSpeechRecognition()
            voiceToggle.isChecked = false
        }
    }

    override fun onSensorChanged(event: SensorEvent?) {
        if (event == null || !isConnected) return

        if (event.sensor.type == Sensor.TYPE_ACCELEROMETER) {
            System.arraycopy(event.values, 0, latestAcc, 0, 3)
            accX.text = String.format(Locale.US, "X: %.2f", latestAcc[0])
            accY.text = String.format(Locale.US, "Y: %.2f", latestAcc[1])
            accZ.text = String.format(Locale.US, "Z: %.2f", latestAcc[2])
        } else if (event.sensor.type == Sensor.TYPE_GYROSCOPE) {
            System.arraycopy(event.values, 0, latestGyro, 0, 3)
            gyroX.text = String.format(Locale.US, "X: %.2f", latestGyro[0])
            gyroY.text = String.format(Locale.US, "Y: %.2f", latestGyro[1])
            gyroZ.text = String.format(Locale.US, "Z: %.2f", latestGyro[2])
        }

        // Send raw IMU payload to LSL Bridge
        val payload = """{"acc": [${latestAcc[0]}, ${latestAcc[1]}, ${latestAcc[2]}], "gyro": [${latestGyro[0]}, ${latestGyro[1]}, ${latestGyro[2]}]}"""
        webSocket?.send(payload)
    }

    override fun onTouchEvent(event: android.view.MotionEvent?): Boolean {
        if (event?.action == android.view.MotionEvent.ACTION_DOWN) {
            if (isConnected) {
                webSocket?.send("""{"touch": "tap"}""")
            }
            return true
        }
        return super.onTouchEvent(event)
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {
        // No-op
    }

    private fun checkAudioPermission(): Boolean {
        return ContextCompat.checkSelfPermission(this, android.Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
    }

    private fun requestAudioPermission() {
        ActivityCompat.requestPermissions(this, arrayOf(android.Manifest.permission.RECORD_AUDIO), REQUEST_RECORD_AUDIO_PERMISSION)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_RECORD_AUDIO_PERMISSION) {
            if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                startSpeechRecognition()
            } else {
                voiceToggle.isChecked = false
                Toast.makeText(this, "Permission to record audio is required for voice commands", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun startSpeechRecognition() {
        if (!checkAudioPermission()) {
            requestAudioPermission()
            return
        }
        
        if (speechRecognizer == null) {
            speechRecognizer = SpeechRecognizer.createSpeechRecognizer(this)
            speechRecognizer?.setRecognitionListener(object : RecognitionListener {
                override fun onReadyForSpeech(params: Bundle?) {
                    voiceStatus.text = "Listening..."
                    voiceStatus.setTextColor(resources.getColor(android.R.color.holo_blue_light, theme))
                }
                override fun onBeginningOfSpeech() {}
                override fun onRmsChanged(rmsd: Float) {}
                override fun onBufferReceived(buffer: ByteArray?) {}
                override fun onEndOfSpeech() {}
                override fun onError(error: Int) {
                    if (voiceToggle.isChecked) {
                        runOnUiThread {
                            speechRecognizer?.startListening(getSpeechIntent())
                        }
                    }
                }
                override fun onResults(results: Bundle?) {
                    val matches = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                    if (!matches.isNullOrEmpty()) {
                        val text = matches[0].trim().lowercase(Locale.US)
                        voiceStatus.text = "Heard: \"$text\""
                        
                        if (text.contains("left")) {
                            sendVoiceCommand("left")
                        } else if (text.contains("right")) {
                            sendVoiceCommand("right")
                        } else if (text.contains("fire") || text.contains("shoot")) {
                            sendVoiceCommand("fire")
                        }
                    }
                    if (voiceToggle.isChecked) {
                        runOnUiThread {
                            speechRecognizer?.startListening(getSpeechIntent())
                        }
                    }
                }
                override fun onPartialResults(partialResults: Bundle?) {}
                override fun onEvent(eventType: Int, params: Bundle?) {}
            })
        }
        
        speechRecognizer?.startListening(getSpeechIntent())
    }
    
    private fun getSpeechIntent(): Intent {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.US.toString())
        intent.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
        return intent
    }
    
    private fun stopSpeechRecognition() {
        speechRecognizer?.stopListening()
        speechRecognizer?.destroy()
        speechRecognizer = null
        voiceStatus.text = ""
    }
    
    private fun sendVoiceCommand(cmd: String) {
        if (isConnected) {
            webSocket?.send("""{"voice": "$cmd"}""")
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        disconnect()
    }
}
