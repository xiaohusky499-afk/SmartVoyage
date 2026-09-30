import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

class TestWeatherAgentCard(unittest.TestCase):
    """测试天气 Agent 的代理卡片定义"""

    def test_agent_card_basic(self):
        """验证代理卡片基本信息"""
        from SmartVoyage.a2a_server.weather_server import agent_card

        self.assertEqual(agent_card.name, "WeatherQueryAssistant")
        self.assertIn("天气查询", agent_card.description)
        self.assertEqual(agent_card.url, "http://localhost:5005")
        self.assertGreater(len(agent_card.skills), 0)

    def test_weather_skill_examples(self):
        """验证天气技能示例"""
        from SmartVoyage.a2a_server.weather_server import agent_card

        skill = agent_card.skills[0]
        self.assertIn("weather", skill.name.lower())
        self.assertIsNotNone(skill.description)
        self.assertGreater(len(skill.examples), 0)

class TestTicketAgentCard(unittest.TestCase):
    """测试票务 Agent 的代理卡片定义"""

    def test_agent_card_skills(self):
        """验证票务代理拥有 6 个技能"""
        from SmartVoyage.a2a_server.ticket_server import agent_card

        self.assertEqual(agent_card.name, "TicketAssistant")
        self.assertEqual(agent_card.url, "http://localhost:5006")
        self.assertEqual(len(agent_card.skills), 6)

    def test_has_query_and_order_skills(self):
        """验证同时包含查询和预定技能"""
        from SmartVoyage.a2a_server.ticket_server import agent_card

        skill_names = [s.name for s in agent_card.skills]
        query_skills = [n for n in skill_names if 'query' in n.lower()]
        order_skills = [n for n in skill_names if 'order' in n.lower()]
        self.assertEqual(len(query_skills), 3, "应该有3个查询技能")
        self.assertEqual(len(order_skills), 3, "应该有3个预定技能")

class TestTripAgentCard(unittest.TestCase):
    """测试行程 Agent 的代理卡片定义"""

    def test_agent_card_skills(self):
        """验证行程代理拥有 6 个技能"""
        from SmartVoyage.a2a_server.trip_server import agent_card

        self.assertEqual(agent_card.name, "TripAssistant")
        self.assertEqual(agent_card.url, "http://localhost:5007")
        self.assertEqual(len(agent_card.skills), 6)

    def test_has_all_trip_skills(self):
        """验证包含租车、旅游团、保险的查询和预定技能"""
        from SmartVoyage.a2a_server.trip_server import agent_card

        skill_names = [s.name.lower() for s in agent_card.skills]
        self.assertTrue(any('car' in n for n in skill_names), "缺少租车技能")
        self.assertTrue(any('tour' in n for n in skill_names), "缺少旅游团技能")
        self.assertTrue(any('insurance' in n for n in skill_names), "缺少保险技能")

'''
# $env:PYTHONPATH = "."
# 运行整个测试模块
python -m  unittest SmartVoyage.tests.test_agent_cards -v

# 运行单个测试类
#测试天气AgentCard
python -m unittest SmartVoyage.tests.test_agent_cards.TestWeatherAgentCard -v

#测试票务AgentCard

python -m unittest SmartVoyage.tests.test_agent_cards.TestTicketAgentCard -v

#测试行程AgentCard
python -m unittest SmartVoyage.tests.test_agent_cards.TestTripAgentCard -v

#测试行程AgentCard 的一个测试方法test_agent_card_skills
python -m unittest SmartVoyage.tests.test_agent_cards.TestTripAgentCard.test_agent_card_skills -v
'''