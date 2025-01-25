FROM python:3.11.3-bullseye

RUN apt update

COPY server server
WORKDIR /server

RUN pip3 install --upgrade pip && \
    pip3 install -r requirements.txt

# Copy the shell script into the container
COPY start_services.sh /start_services.sh

# Give execute permissions to the script
RUN chmod +x /start_services.sh

# Use the shell script as the entry point
ENTRYPOINT ["/start_services.sh"]
