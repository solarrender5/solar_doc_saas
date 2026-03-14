FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Set non-interactive mode to prevent apt-get from prompting for user input
ENV DEBIAN_FRONTEND=noninteractive

# 1. Enable the 'contrib' repository so apt-get can find ttf-mscorefonts-installer
# We conditionally check for both Debian 12 (debian.sources) and older Debian formats (sources.list)
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/Components: main/Components: main contrib/g' /etc/apt/sources.list.d/debian.sources; \
    fi && \
    if[ -f /etc/apt/sources.list ]; then \
        sed -i 's/main/main contrib/g' /etc/apt/sources.list; \
    fi

# 2. Pre-accept the Microsoft TrueType core fonts EULA to prevent the build from hanging
RUN echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections

# 3. Run the installation (now it will succeed quietly)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    libreoffice-writer \
    ttf-mscorefonts-installer \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

# Add your custom fonts
RUN mkdir -p /usr/share/fonts/truetype/custom
COPY ./fonts/ /usr/share/fonts/truetype/custom/
RUN fc-cache -f -v

# App Setup
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

COPY . /app/
EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]