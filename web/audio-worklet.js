class StreamBuffer {
  constructor() {
    this.samples = new Float32Array(8000 * 8);
    this.head = 0;
    this.tail = 0;
    this.length = 0;
    this.position = 0;
    this.buffering = true;
    this.flush = false;
  }

  push(buffer) {
    for (const sample of new Int16Array(buffer)) {
      if (this.length === this.samples.length) {
        this.tail = (this.tail + 1) % this.samples.length;
        this.length--;
      }
      this.samples[this.head] = sample / 32768;
      this.head = (this.head + 1) % this.samples.length;
      this.length++;
    }
  }

  next(step) {
    if (this.buffering && (this.length >= 1600 || (this.flush && this.length > 0))) {
      this.buffering = false;
    }
    if (this.buffering || this.length === 0) return null;
    const first = this.samples[this.tail];
    const second = this.length > 1 ? this.samples[(this.tail + 1) % this.samples.length] : first;
    const value = first + (second - first) * this.position;
    this.position += step;
    while (this.position >= 1 && this.length > 0) {
      this.position -= 1;
      this.tail = (this.tail + 1) % this.samples.length;
      this.length--;
    }
    if (this.length === 0) {
      this.buffering = true;
      this.flush = false;
      this.position = 0;
    }
    return value;
  }
}

class HoselineAudioProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.streams = new Map();
    this.port.onmessage = (event) => {
      const { type, key, buffer } = event.data;
      if (type === "reset") {
        if (key === undefined) this.streams.clear();
        else this.streams.delete(key);
      } else if (type === "flush") {
        const stream = this.streams.get(key);
        if (stream) stream.flush = true;
      } else if (type === "audio") {
        if (!this.streams.has(key)) this.streams.set(key, new StreamBuffer());
        this.streams.get(key).push(buffer);
      }
    };
  }

  process(_inputs, outputs) {
    const channels = outputs[0];
    const output = channels[0];
    const streams = [...this.streams.values()];
    const step = 8000 / sampleRate;
    for (let index = 0; index < output.length; index++) {
      let mixed = 0;
      let count = 0;
      for (const stream of streams) {
        const sample = stream.next(step);
        if (sample !== null) {
          mixed += sample;
          count++;
        }
      }
      const value = count ? mixed / count : 0;
      for (const channel of channels) channel[index] = value;
    }
    return true;
  }
}

registerProcessor("hoseline-audio", HoselineAudioProcessor);
