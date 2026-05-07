#!/usr/bin/env bash
set -e

echo "Installing Flask..."
sudo apt install -y python3-flask

echo "Installing systemd service..."
sudo tee /etc/systemd/system/et-decisions.service > /dev/null << 'EOF'
[Unit]
Description=Employment Tribunal Decisions Web Interface
After=network.target

[Service]
Type=simple
User=dave
WorkingDirectory=/home/dave/employment-tribunal-decisions
ExecStart=/usr/bin/python3 /home/dave/employment-tribunal-decisions/web.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable et-decisions
sudo systemctl start et-decisions

echo ""
echo "Done. Service is running."
echo "  Local:   http://localhost:8080"
echo "  Network: http://192.168.0.104:8080"
echo ""
echo "Logs: sudo journalctl -u et-decisions -f"
