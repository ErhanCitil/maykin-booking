from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.DashboardHome.as_view(), name="home"),
    path("hotels/nieuw/", views.HotelCreate.as_view(), name="hotel_create"),
    path("hotels/<int:pk>/", views.HotelUpdate.as_view(), name="hotel_edit"),
    path(
        "hotels/<int:pk>/verwijderen/",
        views.HotelDelete.as_view(),
        name="hotel_delete",
    ),
    path(
        "hotels/<int:hotel_pk>/kamers/nieuw/",
        views.RoomCreate.as_view(),
        name="room_create",
    ),
    path("kamers/<int:pk>/", views.RoomUpdate.as_view(), name="room_edit"),
    path(
        "kamers/<int:pk>/verwijderen/",
        views.RoomDelete.as_view(),
        name="room_delete",
    ),
    path(
        "kamers/<int:pk>/blokkades/",
        views.RoomBlocks.as_view(),
        name="room_blocks",
    ),
    path(
        "blokkades/<int:pk>/verwijderen/",
        views.RoomBlockDelete.as_view(),
        name="room_block_delete",
    ),
    path("steden/nieuw/", views.CityCreate.as_view(), name="city_create"),
    path("boekingen/", views.BookingList.as_view(), name="booking_list"),
    path(
        "boekingen/export.csv",
        views.BookingExport.as_view(),
        name="booking_export",
    ),
    path(
        "boekingen/<int:pk>/",
        views.BookingDetail.as_view(),
        name="booking_detail",
    ),
    path(
        "boekingen/<int:pk>/annuleren/",
        views.BookingCancel.as_view(),
        name="booking_cancel",
    ),
    path(
        "boekingen/<int:pk>/mail-opnieuw/",
        views.BookingResend.as_view(),
        name="booking_resend",
    ),
    path(
        "boekingen/<int:pk>/terugbetaling/",
        views.BookingRefundRetry.as_view(),
        name="booking_refund_retry",
    ),
    path("prijsregels/", views.PricingRulesView.as_view(), name="pricing_rules"),
    path(
        "prijsregels/seizoenen/nieuw/",
        views.SeasonCreate.as_view(),
        name="season_create",
    ),
    path(
        "prijsregels/seizoenen/<int:pk>/",
        views.SeasonUpdate.as_view(),
        name="season_edit",
    ),
    path(
        "prijsregels/seizoenen/<int:pk>/verwijderen/",
        views.SeasonDelete.as_view(),
        name="season_delete",
    ),
    path("kortingscodes/", views.DiscountList.as_view(), name="discount_list"),
    path(
        "kortingscodes/nieuw/",
        views.DiscountCreate.as_view(),
        name="discount_create",
    ),
    path(
        "kortingscodes/<int:pk>/",
        views.DiscountUpdate.as_view(),
        name="discount_edit",
    ),
    path("facturen/", views.InvoiceList.as_view(), name="invoice_list"),
    path(
        "facturen/export.csv",
        views.InvoiceExport.as_view(),
        name="invoice_export",
    ),
    path(
        "facturen/<int:pk>/pdf/",
        views.InvoicePdf.as_view(),
        name="invoice_pdf",
    ),
    path("klanten/", views.CustomerList.as_view(), name="customer_list"),
    path(
        "klanten/export.csv",
        views.CustomerExport.as_view(),
        name="customer_export",
    ),
    path(
        "klanten/<int:order_pk>/",
        views.CustomerDetail.as_view(),
        name="customer_detail",
    ),
    path("rapportage/", views.Report.as_view(), name="report"),
]
