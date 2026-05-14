# Use an official Python runtime as a parent image
FROM python:3.11-buster

# Set the working directory in the container
WORKDIR /app

# Install necessary system packages and build tools
RUN apt-get update && apt-get install -y \
    build-essential \
    vlc \
    python3-pyqt5 \
    libasound2 \
    libxkbcommon-x11-0 \
    libxcb-xinerama0 \
    libxcb-xinput0 \
    libxcb-xkb1 \
    libx11-xcb1 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render-util0 \
    libxcb-shape0 \
    libxcb-shm0 \
    libxcb-sync1 \
    libxcb-xfixes0 \
    libxcb1 \
    libfontconfig1 \
    libdbus-1-3 \
    libgl1-mesa-glx \
    libgl1-mesa-dri \
    libsm6 \
    libice6 \
    libxt6 \
    libxrender1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxi6 \
    libxrandr2 \
    libxxf86vm1 \
    libxinerama1 \
    libgl1 \
    libegl1 \
    libwayland-client0 \
    libwayland-server0 \
    libx11-dev \
    xvfb \
    x11-xkb-utils \
    xfonts-100dpi \
    xfonts-75dpi \
    xfonts-base \
    xfonts-scalable \
    x11-apps \
    pulseaudio \
    libpulse-dev \
    sudo \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user and set permissions
RUN useradd -m appuser && echo "appuser:password" | chpasswd && adduser appuser sudo

# Set environment variables for runtime
ENV XDG_RUNTIME_DIR=/home/appuser/.runtime
ENV QT_QPA_PLATFORM=offscreen
ENV QT_QPA_PLATFORM_PLUGIN_PATH=/usr/lib/x86_64-linux-gnu/qt5/plugins/platforms

# Create necessary directories with correct permissions
RUN mkdir -p /tmp/.X11-unix /home/appuser/.runtime && \
    chmod 1777 /tmp/.X11-unix && \
    chmod 0700 /home/appuser/.runtime

# Switch to non-root user
USER appuser

# Copy the current directory contents into the container at /app
COPY . /app

# Upgrade pip and install build tools
RUN pip install --upgrade pip setuptools wheel

# Install the package using setup.py with no build isolation and verbose output
RUN pip install . --no-build-isolation --verbose

# Run the application with Xvfb and start PulseAudio
CMD ["sh", "-c", "Xvfb :99 -screen 0 1024x768x24 & export DISPLAY=:99 && pulseaudio --start --log-target=syslog && python surgui/vidPlayer.py"]
