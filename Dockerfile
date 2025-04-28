FROM fedora:42

# Install dependencies and Python 3.9
RUN dnf -y update && \
    dnf -y install \
    g++ make wget pv git bash xz \
    python3.9 python3-pip \
    mediainfo psmisc procps-ng supervisor && \
    dnf clean all

# Set Python 3.9 as default
RUN alternatives --install /usr/bin/python3 python3 /usr/bin/python3.9 1 && \
    alternatives --set python3 /usr/bin/python3.9 && \
    python3 --version

# Environment variables
ENV SUPERVISORD_CONF_DIR=/etc/supervisor/conf.d
ENV SUPERVISORD_LOG_DIR=/var/log/supervisor

# Create necessary directories
RUN mkdir -p ${SUPERVISORD_CONF_DIR} \
    ${SUPERVISORD_LOG_DIR} \
    /app

# Set working directory
WORKDIR /app

# Copy and install scripts and dependencies
COPY install.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/install.sh

COPY requirements.txt ./
RUN echo "supervisor" >> requirements.txt
RUN python3 -m pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose application port
EXPOSE 5000

# Remove changes if architecture is ARM (optional logic)
RUN if [[ $(arch) == 'aarch64' ]]; then   dnf -qq -y history undo last; fi && dnf clean all

# Set the application entry point
CMD ["python3", "cluster.py"]
