# Light sensor rig: cloud MQTT broker (Mosquitto, MQTT over WebSockets).
# Railway terminates TLS at its edge, so clients connect with wss://<this service's domain>.
FROM eclipse-mosquitto:2.0.18
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
