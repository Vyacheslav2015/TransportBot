#!/usr/bin/env python3

import requests
import sys
import json
from datetime import datetime

class TransportBotAPITester:
    def __init__(self, base_url="https://cargo-tracker-260.preview.emergentagent.com"):
        self.base_url = base_url
        self.tests_run = 0
        self.tests_passed = 0
        self.test_results = []

    def log_test(self, name, success, details=""):
        """Log test result"""
        self.tests_run += 1
        if success:
            self.tests_passed += 1
            print(f"✅ {name} - PASSED")
        else:
            print(f"❌ {name} - FAILED: {details}")
        
        self.test_results.append({
            "test": name,
            "success": success,
            "details": details
        })

    def run_test(self, name, method, endpoint, expected_status=200, data=None):
        """Run a single API test"""
        url = f"{self.base_url}/{endpoint}"
        headers = {'Content-Type': 'application/json'}

        print(f"\n🔍 Testing {name}...")
        print(f"   URL: {url}")
        
        try:
            if method == 'GET':
                response = requests.get(url, headers=headers, timeout=10)
            elif method == 'POST':
                response = requests.post(url, json=data, headers=headers, timeout=10)
            else:
                self.log_test(name, False, f"Unsupported method: {method}")
                return False, {}

            print(f"   Status: {response.status_code}")
            
            success = response.status_code == expected_status
            
            if success:
                try:
                    response_data = response.json()
                    self.log_test(name, True, f"Status {response.status_code}")
                    return True, response_data
                except json.JSONDecodeError:
                    self.log_test(name, False, "Invalid JSON response")
                    return False, {}
            else:
                try:
                    error_data = response.json()
                    self.log_test(name, False, f"Status {response.status_code}: {error_data}")
                except:
                    self.log_test(name, False, f"Status {response.status_code}: {response.text[:200]}")
                return False, {}

        except requests.exceptions.Timeout:
            self.log_test(name, False, "Request timeout")
            return False, {}
        except requests.exceptions.ConnectionError:
            self.log_test(name, False, "Connection error")
            return False, {}
        except Exception as e:
            self.log_test(name, False, f"Exception: {str(e)}")
            return False, {}

    def test_bot_status(self):
        """Test GET /api/bot/status"""
        success, response = self.run_test(
            "Bot Status API",
            "GET",
            "api/bot/status",
            200
        )
        
        if success:
            # Validate response structure
            required_fields = ['status', 'timestamp']
            missing_fields = [field for field in required_fields if field not in response]
            
            if missing_fields:
                self.log_test("Bot Status Response Structure", False, f"Missing fields: {missing_fields}")
                return False
            else:
                self.log_test("Bot Status Response Structure", True)
                print(f"   Bot Status: {response.get('status', 'unknown')}")
                print(f"   Uptime: {response.get('uptime', 0)} seconds")
                return True
        return False

    def test_bot_config(self):
        """Test GET /api/bot/config"""
        success, response = self.run_test(
            "Bot Config API",
            "GET",
            "api/bot/config",
            200
        )
        
        if success:
            # Validate that sensitive data is not exposed
            sensitive_fields = ['botToken', 'llmApiKey']
            exposed_fields = [field for field in sensitive_fields if field in response]
            
            if exposed_fields:
                self.log_test("Bot Config Security", False, f"Sensitive fields exposed: {exposed_fields}")
                return False
            else:
                self.log_test("Bot Config Security", True, "No sensitive data exposed")
                
                # Check for expected safe fields
                expected_fields = ['botTokenSet', 'llmApiKeySet']
                present_fields = [field for field in expected_fields if field in response]
                print(f"   Config fields present: {list(response.keys())}")
                return True
        return False

    def test_bot_logs(self):
        """Test GET /api/bot/logs"""
        success, response = self.run_test(
            "Bot Logs API",
            "GET",
            "api/bot/logs",
            200
        )
        
        if success:
            # Validate response structure
            if isinstance(response, dict) and 'stdout' in response and 'stderr' in response:
                self.log_test("Bot Logs Response Structure", True)
                stdout_count = len(response.get('stdout', []))
                stderr_count = len(response.get('stderr', []))
                print(f"   Stdout logs: {stdout_count} lines")
                print(f"   Stderr logs: {stderr_count} lines")
                return True
            else:
                self.log_test("Bot Logs Response Structure", False, "Invalid logs structure")
                return False
        return False

    def test_bot_restart(self):
        """Test POST /api/bot/restart"""
        success, response = self.run_test(
            "Bot Restart API",
            "POST",
            "api/bot/restart",
            200
        )
        
        if success:
            # Validate response structure
            if 'success' in response:
                self.log_test("Bot Restart Response Structure", True)
                restart_success = response.get('success', False)
                print(f"   Restart success: {restart_success}")
                if restart_success:
                    print(f"   Output: {response.get('output', 'No output')}")
                else:
                    print(f"   Error: {response.get('error', 'Unknown error')}")
                return True
            else:
                self.log_test("Bot Restart Response Structure", False, "Missing 'success' field")
                return False
        return False

    def test_api_root(self):
        """Test GET /api/ root endpoint"""
        success, response = self.run_test(
            "API Root",
            "GET",
            "api/",
            200
        )
        
        if success and 'message' in response:
            self.log_test("API Root Response", True)
            print(f"   Message: {response.get('message')}")
            return True
        elif success:
            self.log_test("API Root Response", False, "Missing 'message' field")
            return False
        return False

    def run_all_tests(self):
        """Run all API tests"""
        print("=" * 60)
        print("🚀 TRANSPORT BOT API TESTING")
        print("=" * 60)
        print(f"Base URL: {self.base_url}")
        print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print()

        # Test all endpoints
        self.test_api_root()
        self.test_bot_status()
        self.test_bot_config()
        self.test_bot_logs()
        self.test_bot_restart()

        # Print summary
        print("\n" + "=" * 60)
        print("📊 TEST SUMMARY")
        print("=" * 60)
        print(f"Tests run: {self.tests_run}")
        print(f"Tests passed: {self.tests_passed}")
        print(f"Tests failed: {self.tests_run - self.tests_passed}")
        print(f"Success rate: {(self.tests_passed/self.tests_run*100):.1f}%" if self.tests_run > 0 else "0%")
        
        if self.tests_passed == self.tests_run:
            print("\n🎉 ALL TESTS PASSED!")
            return 0
        else:
            print(f"\n⚠️  {self.tests_run - self.tests_passed} TEST(S) FAILED")
            return 1

def main():
    tester = TransportBotAPITester()
    return tester.run_all_tests()

if __name__ == "__main__":
    sys.exit(main())