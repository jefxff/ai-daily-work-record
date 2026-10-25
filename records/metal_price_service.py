import requests
from bs4 import BeautifulSoup
from django.utils import timezone
import re
import logging
from .error_handler import ErrorHandler

# 配置日志
logger = logging.getLogger(__name__)


def crawl_metal_prices():
    """
    从长江有色金属网爬取金属价格数据
    改进：添加重试机制和错误处理
    网址: https://www.ccmn.cn/priceinfo/
    使用CSS选择器: body > div.contentBar > div > div.content1 > div.fluctuat > div.fluctuat_box.left > div.fluctuat_inside
    """
    url = 'https://www.ccmn.cn/priceinfo/'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    }
    
    metal_prices = []
    
    # 目标金属列表及其单位
    target_metals = {
        '1#铜': '元/吨',
        'A00铝': '元/吨',
        '1#锌': '元/吨',
        '0#锌': '元/吨',
        '1#铅': '元/吨',
        '1#锡': '元/吨',
        '1#镍': '元/吨',
        '1#白银': '元/千克',
        '钯金': '元/克',
        '1#钴': '元/吨',
        '1#锑': '元/吨',
        '2#锑': '元/吨',
        '1#镁': '元/吨',
        '1#电解锰': '元/吨',
    }
    
    # 使用带重试机制的会话
    session = ErrorHandler.create_retry_session()
    
    try:
        response = session.get(url, headers=headers, timeout=30)
        response.encoding = 'utf-8'
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 使用CSS选择器定位元素
            fluctuat_inside = soup.select_one('body > div.contentBar > div > div.content1 > div.fluctuat > div.fluctuat_box.left > div.fluctuat_inside')
            
            if fluctuat_inside:
                logger.info("成功定位到fluctuat_inside元素")
                
                # 查找所有包含金属价格的元素（class="fluctuat_content"的div）
                metal_items = fluctuat_inside.find_all('div', class_='fluctuat_content')
                
                logger.info(f"找到 {len(metal_items)} 个金属元素")
                
                # 提取金属名称和价格
                for item in metal_items:
                    try:
                        # 提取金属名称
                        metal_link = item.find('a')
                        if not metal_link:
                            continue
                        metal_name = metal_link.get_text(strip=True)
                        
                        # 检查是否是目标金属
                        if metal_name not in target_metals:
                            continue
                        
                        # 提取价格（class="fluctuat_number"的label）
                        price_label = item.find('label', class_='fluctuat_number')
                        if not price_label:
                            continue
                        price_text = price_label.get_text(strip=True)
                        price_str = price_text.replace(',', '')
                        price = float(price_str)
                        
                        # 提取涨跌
                        price_change = 0
                        change_labels = item.find_all('label')
                        if len(change_labels) >= 2:
                            # 第二个label应该是涨跌值
                            change_text = change_labels[1].get_text(strip=True)
                            try:
                                price_change = float(change_text.replace(',', ''))
                            except:
                                pass
                        
                        metal_prices.append({
                            'metal_name': metal_name,
                            'price': price,
                            'unit': target_metals[metal_name],
                            'price_change': price_change,
                            'crawl_date': timezone.localdate(),
                            'crawl_time': timezone.localtime()
                        })
                        logger.info(f"提取到 {metal_name}: {price} {target_metals[metal_name]} (涨跌: {price_change})")
                    except Exception as e:
                        logger.warning(f"提取金属价格时出错: {str(e)}")
            
            # 如果通过CSS选择器没有找到数据，尝试从整个页面中提取
            if not metal_prices:
                logger.warning("CSS选择器未找到数据，尝试从整个页面提取")
                
                # 从整个页面中查找金属数据
                all_text = soup.get_text()
                
                for metal_name in target_metals.keys():
                    # 查找金属名称附近的价格
                    pattern = rf'{re.escape(metal_name)}.{{0,100}}?(\d[\d,]*(?:\.\d+)?)'
                    match = re.search(pattern, all_text, re.DOTALL)
                    if match:
                        try:
                            price_str = match.group(1).replace(',', '')
                            price = float(price_str)
                            
                            metal_prices.append({
                                'metal_name': metal_name,
                                'price': price,
                                'unit': target_metals[metal_name],
                                'price_change': 0,
                                'crawl_date': timezone.localdate(),
                                'crawl_time': timezone.localtime()
                            })
                            logger.info(f"从页面文本提取到 {metal_name}: {price} {target_metals[metal_name]}")
                        except Exception as e:
                            logger.warning(f"提取 {metal_name} 价格时出错: {str(e)}")
            
            logger.info(f"成功从长江有色金属网爬取 {len(metal_prices)} 条金属价格数据")
            
            # 如果没有爬取到数据，使用备用数据
            if not metal_prices:
                logger.warning("未能从网站爬取数据，使用备用数据")
                return get_sample_metal_prices()
                
    except Exception as e:
        logger.warning(f"爬取金属价格时出错: {str(e)}")
        import traceback
        traceback.print_exc()
        logger.info("使用备用数据")
        return get_sample_metal_prices()
    
    return metal_prices


def crawl_gold_price():
    """
    爬取金价网的黄金价格数据
    网址: https://www.jinjia.com.cn/
    使用CSS选择器: body > div:nth-child(3) > div.left > div:nth-child(1) > div > ul > li:nth-child(3) > div.new > span
    返回格式: [{metal_name, price, unit, price_change, crawl_date}, ...]
    """
    url = 'https://www.jinjia.com.cn/'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    }
    
    gold_prices = []
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.encoding = 'utf-8'
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            gold_price = None
            prev_close = None  # 昨收价
            
            # 方法1: 使用提供的CSS选择器
            try:
                price_span = soup.select_one('body > div:nth-child(3) > div.left > div:nth-child(1) > div > ul > li:nth-child(3) > div.new > span')
                if price_span:
                    price_text = price_span.get_text(strip=True)
                    price_match = re.search(r'([\d.]+)', price_text)
                    if price_match:
                        price_val = float(price_match.group(1))
                        # 验证价格合理性（黄金价格通常在300-2000元/克之间）
                        if 300 <= price_val <= 2000:
                            gold_price = price_val
                            logger.info(f"从CSS选择器找到黄金价格: {gold_price}")
                            
                            # 找到对应的li元素，提取昨收价
                            parent_li = price_span.find_parent('li')
                            if parent_li:
                                prec_div = parent_li.find('div', class_='prec')
                                if prec_div:
                                    prec_text = prec_div.get_text(strip=True)
                                    prec_match = re.search(r'([\d.]+)', prec_text)
                                    if prec_match:
                                        prev_close = float(prec_match.group(1))
                                        logger.info(f"从CSS选择器父级找到昨收价: {prev_close}")
            except Exception as e:
                logger.warning(f"使用CSS选择器查找黄金价格时出错: {str(e)}")
            
            # 方法2: 查找ul列表中的黄金价格
            if not gold_price:
                ul_elements = soup.find_all('ul')
                for ul in ul_elements:
                    li_elements = ul.find_all('li')
                    for li in li_elements:
                        text = li.get_text(strip=True)
                        # 匹配"今日金价"或"黄金价格"
                        if '今日金价' in text or '黄金价格' in text:
                            # 提取价格，格式如 "今日金价1149.76-3.08%"
                            price_match = re.search(r'(?:今日金价|黄金价格)([\d.]+)', text)
                            if price_match:
                                price_val = float(price_match.group(1))
                                # 验证价格合理性
                                if 300 <= price_val <= 2000:
                                    gold_price = price_val
                                    logger.info(f"从ul列表找到黄金价格: {gold_price}")
                            
                            # 提取昨收价 (prec class)
                            prec_div = li.find('div', class_='prec')
                            if prec_div:
                                prec_text = prec_div.get_text(strip=True)
                                prec_match = re.search(r'([\d.]+)', prec_text)
                                if prec_match:
                                    prev_close = float(prec_match.group(1))
                                    logger.info(f"找到昨收价: {prev_close}")
                            break
                    if gold_price:
                        break
            
            # 方法3: 查找金店价格（如周大福、老凤祥等）
            if not gold_price:
                for ul in ul_elements:
                    li_elements = ul.find_all('li')
                    for li in li_elements:
                        text = li.get_text(strip=True)
                        # 匹配金店价格，格式如 "周大福1652元/克"
                        if any(shop in text for shop in ['周大福', '老凤祥', '周生生', '六福', '周大生']):
                            price_match = re.search(r'(\d+)元/克', text)
                            if price_match:
                                price_val = float(price_match.group(1))
                                # 验证价格合理性
                                if 300 <= price_val <= 2000:
                                    gold_price = price_val
                                    logger.info(f"从金店价格找到黄金价格: {gold_price}")
                                    break
                    if gold_price:
                        break
            
            # 方法4: 从页面文本中提取
            if not gold_price:
                all_text = soup.get_text()
                # 查找黄金价格模式
                patterns = [
                    r'黄金价格[^\d]*(\d+\.?\d*)\s*元/克',
                    r'黄金[^\d]*(\d+\.?\d*)\s*元/克',
                    r'金价[^\d]*(\d+\.?\d*)\s*元/克',
                ]
                for pattern in patterns:
                    match = re.search(pattern, all_text)
                    if match:
                        price_val = float(match.group(1))
                        if 300 <= price_val <= 2000:
                            gold_price = price_val
                            logger.info(f"从页面文本找到黄金价格: {gold_price}")
                            break
            
            # 如果找到黄金价格，添加到结果
            if gold_price:
                # 计算涨跌：如果有昨收价则比对昨收价，否则查询数据库中的昨天价格
                price_change = 0
                if prev_close:
                    price_change = gold_price - prev_close
                    logger.info(f"根据昨收价计算涨跌: {prev_close} -> {gold_price}, 涨跌: {price_change}")
                else:
                    # 尝试从数据库获取昨天的价格来计算涨跌
                    from records.models import MetalPrice
                    from datetime import timedelta
                    yesterday = timezone.localdate() - timedelta(days=1)
                    yesterday_gold = MetalPrice.objects.filter(
                        metal_name='黄金',
                        crawl_date=yesterday
                    ).first()
                    if yesterday_gold:
                        price_change = gold_price - float(yesterday_gold.price)
                        logger.info(f"根据昨日价格计算涨跌: {yesterday_gold.price} -> {gold_price}, 涨跌: {price_change}")
                
                gold_prices.append({
                    'metal_name': '黄金',
                    'price': gold_price,
                    'unit': '元/克',
                    'price_change': round(price_change, 2),
                    'crawl_date': timezone.localdate(),
                    'crawl_time': timezone.localtime()
                })
                logger.info(f"成功获取黄金价格: {gold_price} 元/克, 涨跌: {round(price_change, 2)}")
            else:
                logger.warning("未找到黄金价格，使用默认值（示例数据）")
                gold_prices.append({
                    'metal_name': '黄金',
                    'price': None,  # None 表示数据无效（爬取失败时的占位）
                    'unit': '元/克',
                    'price_change': 0,
                    'crawl_date': timezone.localdate(),
                    'crawl_time': timezone.localtime(),
                    'is_sample': True  # 标记为示例数据，前端可据此判断
                })
        
    except Exception as e:
        logger.warning(f"爬取黄金价格时出错: {str(e)}")
        import traceback
        traceback.print_exc()
        # 出错时使用示例数据占位，不写入硬编码价格
        gold_prices.append({
            'metal_name': '黄金',
            'price': None,
            'unit': '元/克',
            'price_change': 0,
            'crawl_date': timezone.localdate(),
            'crawl_time': timezone.localtime(),
            'is_sample': True
        })
    
    return gold_prices


def get_sample_metal_prices():
    """
    获取示例金属价格数据（用于测试或当爬取失败时）
    基于长江有色金属网的实际数据
    """
    sample_data = [
        {'metal_name': '1#铜', 'price': 101910, 'unit': '元/吨', 'price_change': -290},
        {'metal_name': 'A00铝', 'price': 23950, 'unit': '元/吨', 'price_change': 330},
        {'metal_name': '0#锌', 'price': 24450, 'unit': '元/吨', 'price_change': 50},
        {'metal_name': '1#锌', 'price': 24350, 'unit': '元/吨', 'price_change': 50},
        {'metal_name': '1#铅', 'price': 16800, 'unit': '元/吨', 'price_change': 25},
        {'metal_name': '1#锡', 'price': 406750, 'unit': '元/吨', 'price_change': -27000},
        {'metal_name': '1#镍', 'price': 139100, 'unit': '元/吨', 'price_change': -2700},
        {'metal_name': '1#白银', 'price': 23280, 'unit': '元/千克', 'price_change': -1300},
        {'metal_name': '钯金', 'price': 420, 'unit': '元/克', 'price_change': -11},
        {'metal_name': '黄金', 'price': 678.90, 'unit': '元/克', 'price_change': 0},
        {'metal_name': '1#钴', 'price': 435000, 'unit': '元/吨', 'price_change': -5000},
        {'metal_name': '1#锑', 'price': 168000, 'unit': '元/吨', 'price_change': 1000},
        {'metal_name': '2#锑', 'price': 163000, 'unit': '元/吨', 'price_change': 1000},
        {'metal_name': '1#镁', 'price': 18100, 'unit': '元/吨', 'price_change': 0},
        {'metal_name': '1#电解锰', 'price': 18800, 'unit': '元/吨', 'price_change': 100},
    ]
    
    # 添加爬取时间
    now = timezone.localtime()
    for item in sample_data:
        item['crawl_date'] = now.date()
        item['crawl_time'] = now
    
    return sample_data


def save_metal_prices_to_db(prices_data):
    """将价格数据保存到数据库
    改进策略：保留30天历史数据，清理更早的数据
    """
    from .models import MetalPrice
    from datetime import timedelta
    
    saved_prices = []
    today = timezone.localdate()
    
    # 清理30天前的历史数据（保留30天历史）
    cutoff_date = today - timedelta(days=30)
    deleted_count, _ = MetalPrice.objects.filter(crawl_date__lt=cutoff_date).delete()
    if deleted_count > 0:
        logger.info(f"清理历史数据: 删除 {cutoff_date} 之前的 {deleted_count} 条记录")
    
    for data in prices_data:
        try:
            metal_price, created = MetalPrice.objects.update_or_create(
                metal_name=data['metal_name'],
                crawl_date=data['crawl_date'],
                defaults={
                    'price': data['price'],
                    'unit': data['unit'],
                    'price_change': data.get('price_change', 0),
                    'crawl_time': data['crawl_time']
                }
            )
            saved_prices.append(metal_price)
        except Exception as e:
            logger.warning(f"保存 {data['metal_name']} 价格时出错: {str(e)}")
    
    return saved_prices


def get_latest_metal_prices():
    """获取最新的金属价格数据"""
    from .models import MetalPrice
    from django.db.models import Max
    
    # 获取最新爬取日期
    latest_date = MetalPrice.objects.aggregate(Max('crawl_date'))['crawl_date__max']
    
    if latest_date:
        return MetalPrice.objects.filter(crawl_date=latest_date).order_by('-price')
    return []


def get_metal_price_history(metal_name, days=30):
    """获取指定金属的价格历史"""
    from .models import MetalPrice
    from datetime import timedelta
    
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=days)
    
    return MetalPrice.objects.filter(
        metal_name=metal_name,
        crawl_date__range=[start_date, end_date]
    ).order_by('crawl_date')


def clean_old_prices():
    """
    清理旧的金属价格数据，每天只保留最新的一条记录
    """
    from .models import MetalPrice
    
    today = timezone.localdate()
    
    # 获取所有金属名称
    metal_names = MetalPrice.objects.values_list('metal_name', flat=True).distinct()
    
    for metal_name in metal_names:
        # 获取该金属今天的所有记录
        today_prices = MetalPrice.objects.filter(
            metal_name=metal_name,
            crawl_date=today
        ).order_by('-crawl_time')
        
        # 保留最新的一条，删除其他的
        if today_prices.count() > 1:
            # 保留第一条（最新的），删除其他
            for price in today_prices[1:]:
                price.delete()
    
    logger.info("已清理旧的金属价格数据，每个金属每天只保留最新的一条记录")


def fetch_and_save_prices(use_sample=False):
    """
    获取并保存金属价格的主函数
    use_sample: 如果为True，使用示例数据；否则尝试爬取
    每天只保留最新的数据，刷新时会删除当天的旧数据并保存新数据
    """
    from .models import MetalPrice
    
    today = timezone.localdate()
    
    logger.info(f"开始获取金属价格数据，日期: {today}")
    
    # 先删除当天的所有旧数据，确保每天只保留最新的一次刷新数据
    deleted_count, _ = MetalPrice.objects.filter(crawl_date=today).delete()
    if deleted_count > 0:
        logger.info(f"已删除当天的 {deleted_count} 条旧价格数据")
    
    if use_sample:
        prices_data = get_sample_metal_prices()
    else:
        # 获取其他金属价格
        prices_data = crawl_metal_prices()
        
        # 如果爬取失败，使用示例数据
        if not prices_data:
            logger.warning("金属价格爬取失败，使用备用数据")
            prices_data = get_sample_metal_prices()
        
        # 获取黄金价格
        logger.info("开始获取黄金价格...")
        gold_prices = crawl_gold_price()
        
        # 将黄金价格添加到价格数据中
        if gold_prices:
            # 检查是否已存在黄金价格，如果存在则更新
            existing_gold = [p for p in prices_data if p['metal_name'] == '黄金']
            if existing_gold:
                # 更新现有的黄金价格
                existing_gold[0]['price'] = gold_prices[0]['price']
                existing_gold[0]['price_change'] = gold_prices[0]['price_change']
                logger.info(f"更新黄金价格: {gold_prices[0]['price']} 元/克")
            else:
                # 添加新的黄金价格
                prices_data.extend(gold_prices)
                logger.info(f"添加黄金价格: {gold_prices[0]['price']} 元/克")
    
    # 保存新数据到数据库
    logger.info(f"准备保存 {len(prices_data)} 种金属的价格数据")
    saved_prices = save_metal_prices_to_db(prices_data)
    
    logger.info(f"成功保存 {len(saved_prices)} 种金属的最新价格数据")
    
    return saved_prices
