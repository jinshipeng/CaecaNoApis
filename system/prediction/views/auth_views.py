from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.models import User, Group
from django.db import IntegrityError
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
import random
import string
from PIL import Image, ImageDraw, ImageFont
import io
import os
from datetime import datetime
import logging
from .. import utils


# 配置日志
logger = logging.getLogger(__name__)

def user_login(request):
    """
    用户登录视图函数
    
    功能：
    - 处理用户登录请求
    - 验证用户凭据
    - 登录用户
    - 重定向到仪表盘
    
    参数：
    - request: Django请求对象
    
    返回值：
    - render: 渲染后的登录页面，或重定向到仪表盘
    """
    # 清除所有消息，避免显示来自其他页面的消息
    utils.clear_messages(request)
    
    # 获取用户类型
    user_type = request.POST.get('user_type', 'user') if request.method == 'POST' else 'user'
    
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        
        # 验证用户凭据
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            # 检查用户类型是否匹配
            if user_type == 'user' and user.is_superuser:
                # 员工登录入口不能登录管理员账户
                messages.error(request, '该账户不能在此登录入口登录')
                return render(request, 'login.html', {
                    'user_type': user_type
                })
            elif user_type == 'admin' and not user.is_superuser:
                # 管理员登录入口不能登录普通员工账户
                messages.error(request, '该账户不能在此登录入口登录')
                return render(request, 'login.html', {
                    'user_type': user_type
                })
            else:
                # 用户类型匹配，登录用户
                login(request, user)
                messages.success(request, '登录成功！')
                # 管理员登录后重定向到后端管理页面
                if user_type == 'admin' and user.is_superuser:
                    return redirect('/admin/')
                # 普通用户登录后重定向到仪表盘
                else:
                    return redirect('dashboard')
        else:
            messages.error(request, '用户名或密码错误')
            # 登录失败，保持在当前用户类型页面
            return render(request, 'login.html', {
                'user_type': user_type
            })
    
    # GET请求，显示登录表单
    return render(request, 'login.html')

def user_logout(request):
    """
    用户注销视图函数
    
    功能：
    - 注销用户
    - 重定向到登录页面
    
    参数：
    - request: Django请求对象
    
    返回值：
    - redirect: 重定向到登录页面
    """
    # 注销用户
    logout(request)
    messages.success(request, '退出成功！')
    return redirect('login')

def generate_captcha():
    """
    生成验证码
    
    返回：
    - captcha_text: 验证码文本
    - image: 验证码图片
    """
    # 生成4位随机验证码
    captcha_text = ''.join(random.choices(string.ascii_letters + string.digits, k=4))
    
    # 创建验证码图片
    width, height = 120, 40
    image = Image.new('RGB', (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)
    
    # 绘制干扰线
    for _ in range(5):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        draw.line([(x1, y1), (x2, y2)], fill=(128, 128, 128), width=1)
    
    # 绘制干扰点
    for _ in range(50):
        x = random.randint(0, width)
        y = random.randint(0, height)
        draw.point((x, y), fill=(128, 128, 128))
    
    # 绘制验证码文本
    try:
        # 尝试使用系统字体
        font = ImageFont.truetype('arial.ttf', 24)
    except (IOError, OSError):
        # 如果系统没有arial字体，使用默认字体
        font = ImageFont.load_default()
    
    # 计算文本位置
    # 使用新的方法计算文本大小，兼容较新的PIL版本
    bbox = draw.textbbox((0, 0), captcha_text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (width - text_width) // 2
    y = (height - text_height) // 2
    
    # 绘制文本
    draw.text((x, y), captcha_text, fill=(0, 0, 0), font=font)
    
    return captcha_text, image

def captcha(request):
    """
    验证码视图函数
    
    返回：
    - HttpResponse: 验证码图片
    """
    # 生成验证码
    captcha_text, image = generate_captcha()
    
    # 将验证码存储在session中
    request.session['captcha'] = captcha_text.lower()
    
    # 将图片转换为HTTP响应
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    buffer.seek(0)
    
    return HttpResponse(buffer.getvalue(), content_type='image/png')

def register(request):
    """
    用户注册视图函数
    
    功能：
    - 处理用户注册请求
    - 创建新用户
    - 根据用户类型设置权限
    - 登录新用户
    - 重定向到仪表盘
    
    参数：
    - request: Django请求对象
    
    返回值：
    - render: 渲染后的登录页面，或重定向到仪表盘
    """
    # 清除所有消息，避免显示来自其他页面的消息
    utils.clear_messages(request)
    
    # 存储用户输入的信息
    form_data = {
        'username': '',
        'email': '',
        'user_type': 'user'
    }
    
    if request.method == 'POST':
        username = request.POST.get('username')
        email = request.POST.get('email')
        password1 = request.POST.get('password1')
        password2 = request.POST.get('password2')
        user_type = request.POST.get('user_type', 'user')
        
        # 存储用户输入的信息
        form_data = {
            'username': username,
            'email': email,
            'password1': password1,
            'password2': password2,
            'user_type': user_type
        }
        
        # 验证表单数据
        if not username or not email or not password1 or not password2:
            messages.error(request, '请填写所有必填字段')
        elif password1 != password2:
            messages.error(request, '两次输入的密码不一致')
        elif len(password1) < 8:
            messages.error(request, '密码长度至少为8位')
        elif not any(char.isdigit() for char in password1):
            messages.error(request, '密码必须包含至少一位数字')
        elif not any(char.isalpha() for char in password1):
            messages.error(request, '密码必须包含至少一位字母')
        elif User.objects.filter(username=username).exists():
            messages.error(request, '用户名已存在')
        elif User.objects.filter(email=email).exists():
            messages.error(request, '邮箱已被注册')
        else:
            # 根据用户类型验证验证码或邀请码
            if user_type == 'user':
                # 验证验证码
                captcha_input = request.POST.get('captcha', '').lower()
                captcha_session = request.session.get('captcha', '')
                if not captcha_input:
                    messages.error(request, '请输入验证码')
                elif captcha_input != captcha_session:
                    messages.error(request, '验证码错误')
                else:
                    # 验证码正确，创建用户
                    try:
                        user = User.objects.create_user(
                            username=username,
                            email=email,
                            password=password1
                        )
                        user.is_staff = False
                        user.is_superuser = False
                        user.save()
                        
                        # 登录新用户
                        login(request, user)
                        messages.success(request, '注册成功！欢迎使用系统。')
                        return redirect('dashboard')
                    except IntegrityError:
                        messages.error(request, '用户名已存在，请使用其他用户名')
                        return render(request, 'login.html', {
                            'show_register': True,
                            'form_data': form_data
                        })
            else:
                # 验证邀请码（从环境变量获取，不再硬编码）
                invite_code = request.POST.get('invite_code', '')
                # 从环境变量或配置文件获取邀请码（必须设置环境变量）
                correct_invite_code = os.environ.get('ADMIN_INVITE_CODE', '')
                if not correct_invite_code:
                    logger.error('管理员邀请码未配置！请设置环境变量 ADMIN_INVITE_CODE')
                    messages.error(request, '系统配置错误：管理员注册功能暂不可用')
                elif not invite_code:
                    messages.error(request, '请输入邀请码')
                elif invite_code != correct_invite_code:
                    # 安全：不提示邀请码错误的具体信息
                    messages.error(request, '邀请码错误，请联系系统管理员获取正确的邀请码')
                else:
                    # 邀请码正确，创建管理员用户
                    try:
                        user = User.objects.create_user(
                            username=username,
                            email=email,
                            password=password1
                        )
                        user.is_staff = True
                        user.is_superuser = True
                        user.save()
                        
                        # 将管理员用户添加到管理员组
                        admin_group, created = Group.objects.get_or_create(name='管理员')
                        user.groups.add(admin_group)
                        
                        # 登录新用户
                        login(request, user)
                        messages.success(request, '管理员注册成功！欢迎使用系统。')
                        # 管理员注册后重定向到后端管理页面
                        return redirect('/admin/')
                    except IntegrityError:
                        messages.error(request, '用户名已存在，请使用其他用户名')
                        return render(request, 'login.html', {
                            'show_register': True,
                            'form_data': form_data
                        })
        
        # 验证失败，渲染登录页面并显示注册表单
        return render(request, 'login.html', {
            'show_register': True,
            'form_data': form_data
        })
    
    # GET请求，重定向到登录页面
    return redirect('login')




