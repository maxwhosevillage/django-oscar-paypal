from django.urls import path

from paypal.express_checkout import views

urlpatterns = [
    path('create-order/', views.CreateOrderView.as_view(), name='express-checkout-create-order'),
    path('capture-order/', views.CaptureOrderView.as_view(), name='express-checkout-capture-order'),
]
