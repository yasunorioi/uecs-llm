.PHONY: install-llm-server install-pi-daemon install-linebot test clean

# LLM制御ループ (x86ミニPC or Pi5向け)
install-llm-server:
	python3 -m venv .venv
	.venv/bin/pip install -e .
	@echo ""
	@echo "=== LLM Server installed ==="
	@echo "Usage: .venv/bin/agriha-control"
	@echo "Cron:  sudo cp systemd/agriha-control.cron /etc/cron.d/agriha-control"
	@echo "LLM:   sudo cp systemd/agriha-llm.service /etc/systemd/system/"

# UniPiデーモン (RPi向け, HWドライバ含む)
install-pi-daemon:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[daemon]"
	@echo ""
	@echo "=== UniPi Daemon installed ==="
	@echo "Usage: .venv/bin/unipi-daemon --config config/unipi_daemon.example.yaml"
	@echo "Systemd: sudo cp systemd/unipi-daemon.service /etc/systemd/system/"

# LINE Bot (Docker/VPS向け)
install-linebot:
	cd linebot && docker compose build
	@echo ""
	@echo "=== LINE Bot built ==="
	@echo "1. cp linebot/.env.example linebot/.env"
	@echo "2. Edit linebot/.env with your credentials"
	@echo "3. cd linebot && docker compose up -d"

# テスト
test:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev]"
	.venv/bin/pytest tests/ -v

clean:
	rm -rf .venv dist build *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
