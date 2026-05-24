app_name = "ecommerce_super"
app_title = "Ecommerce Super"
app_publisher = "Incirco"
app_description = "ERPNext-native EasyEcom integration"
app_email = "nikhil@incirco.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "ecommerce_super",
# 		"logo": "/assets/ecommerce_super/logo.png",
# 		"title": "Ecommerce Super",
# 		"route": "/ecommerce_super",
# 		"has_permission": "ecommerce_super.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/ecommerce_super/css/ecommerce_super.css"
# app_include_js = "/assets/ecommerce_super/js/ecommerce_super.js"

# include js, css files in header of web template
# web_include_css = "/assets/ecommerce_super/css/ecommerce_super.css"
# web_include_js = "/assets/ecommerce_super/js/ecommerce_super.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "ecommerce_super/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "ecommerce_super/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "ecommerce_super.utils.jinja_methods",
# 	"filters": "ecommerce_super.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "ecommerce_super.install.before_install"
# after_install = "ecommerce_super.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "ecommerce_super.uninstall.before_uninstall"
# after_uninstall = "ecommerce_super.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "ecommerce_super.utils.before_app_install"
# after_app_install = "ecommerce_super.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "ecommerce_super.utils.before_app_uninstall"
# after_app_uninstall = "ecommerce_super.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "ecommerce_super.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "ecommerce_super.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"ecommerce_super.tasks.all"
# 	],
# 	"daily": [
# 		"ecommerce_super.tasks.daily"
# 	],
# 	"hourly": [
# 		"ecommerce_super.tasks.hourly"
# 	],
# 	"weekly": [
# 		"ecommerce_super.tasks.weekly"
# 	],
# 	"monthly": [
# 		"ecommerce_super.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "ecommerce_super.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "ecommerce_super.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "ecommerce_super.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "ecommerce_super.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["ecommerce_super.utils.before_request"]
# after_request = ["ecommerce_super.utils.after_request"]

# Job Events
# ----------
# before_job = ["ecommerce_super.utils.before_job"]
# after_job = ["ecommerce_super.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"ecommerce_super.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

