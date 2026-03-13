#!/usr/bin/env python3
"""
Pico 2 W USB自動テストスクリプト

Usage:
    python3 auto_test_runner.py [--notify] [--config CONFIG]

機能:
    - USB接続検知後に自動実行（udev連携）
    - シリアル接続テスト
    - CircuitPython REPL応答確認
    - CPU温度読み取り
    - LINE Notifyで結果通知
"""

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Optional

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("Error: pyserial not installed. Run: pip3 install pyserial")
    sys.exit(1)

from notify import send_line_notify

# ===== ログ設定 =====
LOG_DIR = Path("/var/log/arsprout")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "pico_test.log")
    ]
)
logger = logging.getLogger(__name__)


# ===== データクラス =====
@dataclass
class TestResult:
    """テスト結果"""
    test_id: str
    name: str
    passed: bool
    duration_ms: int
    message: str
    timestamp: str


@dataclass
class TestReport:
    """テストレポート"""
    target: str
    total_tests: int
    passed: int
    failed: int
    duration_ms: int
    results: List[TestResult]
    timestamp: str


# ===== 緊急停止ハンドラ =====
def emergency_shutdown(signum, frame):
    """緊急停止ハンドラ"""
    logger.critical("Emergency shutdown triggered!")
    sys.exit(1)

signal.signal(signal.SIGTERM, emergency_shutdown)
signal.signal(signal.SIGINT, emergency_shutdown)


# ===== テストランナー =====
class USBTestRunner:
    """Pico 2 W USB接続テスト"""

    # Raspberry Pi Foundation Vendor ID
    PICO_VID = 0x2E8A
    # Pico Product IDs
    PICO_PIDS = [0x0005, 0x000A, 0x000B]  # CDC, MicroPython, CircuitPython

    def __init__(self, config: dict):
        self.config = config
        self.results: List[TestResult] = []
        self.port: Optional[str] = None
        self.serial: Optional[serial.Serial] = None

    def run_test(self, test_id: str, name: str, test_func, timeout: float = 10.0) -> TestResult:
        """単一テストの実行"""
        start = time.time()
        try:
            result = test_func()
            passed = True
            message = str(result) if result else "OK"
        except Exception as e:
            passed = False
            message = f"Error: {str(e)}"

        duration = int((time.time() - start) * 1000)

        test_result = TestResult(
            test_id=test_id,
            name=name,
            passed=passed,
            duration_ms=duration,
            message=message,
            timestamp=datetime.now().isoformat()
        )

        self.results.append(test_result)
        status = "PASS" if passed else "FAIL"
        logger.info(f"[{status}] {test_id}: {name} - {message} ({duration}ms)")

        return test_result

    def find_pico_port(self) -> Optional[str]:
        """Picoのシリアルポートを検索"""
        ports = serial.tools.list_ports.comports()
        for port in ports:
            if port.vid == self.PICO_VID:
                logger.info(f"Found Pico device: {port.device} (VID={port.vid:04X}, PID={port.pid:04X})")
                return port.device
            # 追加チェック: 説明文にPicoが含まれる
            if port.description and "Pico" in port.description:
                logger.info(f"Found Pico by description: {port.device}")
                return port.device
        return None

    def test_device_recognition(self) -> str:
        """USB-001: デバイス認識"""
        self.port = self.find_pico_port()
        if not self.port:
            # 全ポートをリストアップしてデバッグ
            all_ports = list(serial.tools.list_ports.comports())
            port_info = [f"{p.device}(VID={p.vid},PID={p.pid})" for p in all_ports]
            raise Exception(f"Pico device not found. Available ports: {port_info}")
        return f"Found at {self.port}"

    def test_serial_connection(self) -> str:
        """USB-002: シリアル接続"""
        if not self.port:
            raise Exception("No port available")

        baudrate = self.config.get("baudrate", 115200)
        self.serial = serial.Serial(
            self.port,
            baudrate,
            timeout=3,
            write_timeout=3
        )
        return f"Connected at {baudrate}bps"

    def test_repl_response(self) -> str:
        """USB-003: REPL応答確認"""
        if not self.serial:
            raise Exception("Serial not connected")

        # バッファクリア
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()

        # Ctrl+C でREPLに割り込み
        self.serial.write(b'\x03')
        time.sleep(0.3)
        self.serial.write(b'\x03')
        time.sleep(0.3)

        # 改行送信
        self.serial.write(b'\r\n')
        time.sleep(0.5)

        # 応答読み取り
        response = self.serial.read(200).decode('utf-8', errors='ignore')

        # CircuitPython/MicroPython プロンプト確認
        if '>>>' in response:
            return "REPL active (CircuitPython/MicroPython)"
        if 'Adafruit' in response or 'CircuitPython' in response:
            return "CircuitPython detected"

        raise Exception(f"No REPL prompt detected. Response: {response[:100]}")

    def test_cpu_temperature(self) -> str:
        """USB-004: CPU温度読み取り"""
        if not self.serial:
            raise Exception("Serial not connected")

        # バッファクリア
        self.serial.reset_input_buffer()

        # CircuitPython用コマンド
        cmd = "import microcontroller; print(microcontroller.cpu.temperature)\r\n"
        self.serial.write(cmd.encode())
        time.sleep(1)

        response = self.serial.read(300).decode('utf-8', errors='ignore')
        logger.debug(f"CPU temp response: {response}")

        # 温度値を抽出
        for line in response.split('\n'):
            line = line.strip()
            try:
                temp = float(line)
                if 10 <= temp <= 85:  # Pico動作温度範囲
                    return f"{temp:.1f}C"
            except ValueError:
                continue

        raise Exception(f"Invalid temperature reading. Response: {response[:100]}")

    def test_echo(self) -> str:
        """USB-005: エコーテスト"""
        if not self.serial:
            raise Exception("Serial not connected")

        # バッファクリア
        self.serial.reset_input_buffer()

        # ユニークなテスト値生成
        test_value = f"ECHO_{int(time.time() * 1000) % 100000}"
        cmd = f"print('{test_value}')\r\n"
        self.serial.write(cmd.encode())
        time.sleep(0.5)

        response = self.serial.read(300).decode('utf-8', errors='ignore')

        if test_value in response:
            return f"Echo OK: {test_value}"
        raise Exception(f"Echo failed. Expected: {test_value}, Got: {response[:100]}")

    def run_all(self) -> TestReport:
        """全テスト実行"""
        start = time.time()
        logger.info("=" * 50)
        logger.info("Starting Pico 2 W USB Test Suite")
        logger.info("=" * 50)

        # テスト実行
        self.run_test("USB-001", "デバイス認識", self.test_device_recognition, 10)
        self.run_test("USB-002", "シリアル接続", self.test_serial_connection, 5)
        self.run_test("USB-003", "REPL応答", self.test_repl_response, 5)
        self.run_test("USB-004", "CPU温度読み取り", self.test_cpu_temperature, 5)
        self.run_test("USB-005", "エコーテスト", self.test_echo, 5)

        # クリーンアップ
        if self.serial:
            self.serial.close()
            logger.info("Serial connection closed")

        passed = sum(1 for r in self.results if r.passed)
        duration = int((time.time() - start) * 1000)

        report = TestReport(
            target="USB (Pico 2 W)",
            total_tests=len(self.results),
            passed=passed,
            failed=len(self.results) - passed,
            duration_ms=duration,
            results=self.results,
            timestamp=datetime.now().isoformat()
        )

        logger.info("=" * 50)
        logger.info(f"Test completed: {passed}/{len(self.results)} passed ({duration}ms)")
        logger.info("=" * 50)

        return report


def format_report_message(report: TestReport) -> str:
    """レポートを通知用メッセージにフォーマット"""
    lines = [
        "",
        f"【{report.target} テスト結果】",
        f"実行時刻: {report.timestamp[:19]}",
        f"結果: {report.passed}/{report.total_tests} 成功",
        ""
    ]

    for r in report.results:
        status = "OK" if r.passed else "NG"
        lines.append(f"{status} {r.test_id}: {r.name}")
        if not r.passed:
            lines.append(f"   -> {r.message}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Pico 2 W Auto Test Runner")
    parser.add_argument("--notify", action="store_true", help="Send LINE notification")
    parser.add_argument("--config", default="test_config.json", help="Config file path")
    args = parser.parse_args()

    # 設定読み込み
    config_path = Path(args.config)
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
        logger.info(f"Config loaded from {config_path}")
    else:
        config = {}
        logger.warning(f"Config file not found: {config_path}, using defaults")

    # テスト実行
    try:
        runner = USBTestRunner(config)
        report = runner.run_all()
    except Exception as e:
        logger.error(f"Test execution failed: {e}")
        return 1

    # 結果表示
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    print(f"Target: {report.target}")
    print(f"Passed: {report.passed}/{report.total_tests}")
    print(f"Duration: {report.duration_ms}ms")
    print("-" * 60)
    for r in report.results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.test_id}: {r.name}")
        if not r.passed:
            print(f"          -> {r.message}")
    print("=" * 60)

    # JSON結果保存
    result_dir = Path(__file__).parent / "results"
    result_dir.mkdir(exist_ok=True)
    result_file = result_dir / f"test_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(result_file, "w") as f:
        json.dump(asdict(report), f, indent=2, ensure_ascii=False)
    logger.info(f"Results saved to {result_file}")

    # LINE通知
    if args.notify:
        token = config.get("notification", {}).get("token")
        if token:
            message = format_report_message(report)
            if send_line_notify(token, message):
                logger.info("LINE notification sent successfully")
            else:
                logger.warning("LINE notification failed")
        else:
            logger.warning("LINE token not configured, skipping notification")

    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
