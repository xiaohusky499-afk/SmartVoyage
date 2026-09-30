import os
from dotenv import load_dotenv

# 项目根目录
project_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
load_dotenv(os.path.join(project_root, '.env'))

# 定义配置文件
class Config:

    def __init__(self):
        # 大模型配置
        self.base_url = os.getenv(
            "DASHSCOPE_API_URL",
            'https://dashscope.aliyuncs.com/compatible-mode/v1',
        )
        self.api_key = os.getenv("DASHSCOPE_API_KEY") # 大模型的key一般也是配到环境变量中,不会写死到代码中的
        self.model_name = os.getenv("DASHSCOPE_MODEL", "qwen3.6-plus")
        self.temperature = 0.1

        # 数据库配置
        self.host = os.getenv('MYSQL_HOST', 'localhost')
        self.user = os.getenv('MYSQL_USER', 'smart_yoyage')
        self.password = os.getenv('MYSQL_PASSWORD')
        self.database = os.getenv('MYSQL_DATABASE', 'travel_rag')
        # 日志配置
        self.log_file = os.path.join(project_root, 'SmartVoyage', 'logs/app.log')

        #意图识别的配置,
        self.intent = {
            "weather": "WeatherQueryAssistant",
            "attraction": "TripAssistant",

            "flight": "TicketAssistant",
            "train": "TicketAssistant",
            "concert": "TicketAssistant",
            "order": "TicketAssistant",

            "car_rental": "TripAssistant",
            "tour_group": "TripAssistant",
            "insurance": "TripAssistant",
            "trip_order": "TripAssistant",
        }

if __name__ == '__main__':
    print(Config().log_file)
