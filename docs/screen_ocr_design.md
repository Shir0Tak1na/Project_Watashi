# Screen OCR Module Design

## Objectives

- allow the user to select a region on the screen with a shortcut
- capture that region in real time
- preprocess the image to improve OCR accuracy
- run OCR asynchronously without blocking the UI thread
- render translation results in a floating window immediately

## Core classes

- ScreenCapturer: capture a screenshot of a selected region
- RegionSelector: manage user selection and hotkey-driven region choice
- ImagePreprocessor: grayscale, thresholding, denoising
- OcrEngine: local OCR engine abstraction
- OcrTaskScheduler: worker-thread pool for asynchronous OCR
- FrameBuffer: double-buffered image cache for low latency
- ScreenRecognitionPipeline: orchestration layer

## Recommended execution model

1. User presses hotkey
2. RegionSelector enters capture mode
3. User drags a selection rectangle
4. ScreenCapturer captures the selected region
5. ImagePreprocessor normalizes the screenshot
6. OcrTaskScheduler runs OCR on a worker thread
7. OCR results return through callback to UI
8. Floating overlay renders translated text

## Key low-latency strategies

- avoid OCR on the UI thread
- use background worker pool for OCR
- swap image buffers instead of copying full frames too often
- downscale or crop unnecessary region before OCR
- cache recent results for repeated text patterns
