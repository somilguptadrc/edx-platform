# -*- coding: utf-8 -*-
"""Tests for course home page date summary blocks."""
from datetime import datetime, timedelta

import ddt
from django.core.urlresolvers import reverse
from freezegun import freeze_time
from nose.plugins.attrib import attr
from pytz import utc

from commerce.models import CommerceConfiguration
from course_modes.models import CourseMode
from course_modes.tests.factories import CourseModeFactory
from courseware.courses import get_course_date_blocks
from courseware.date_summary import (
    CourseEndDate,
    CourseStartDate,
    TodaysDate,
    VerificationDeadlineDate,
    VerifiedUpgradeDeadlineDate
)
from courseware.models import DynamicUpgradeDeadlineConfiguration, CourseDynamicUpgradeDeadlineConfiguration
from lms.djangoapps.verify_student.models import VerificationDeadline
from lms.djangoapps.verify_student.tests.factories import SoftwareSecurePhotoVerificationFactory
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from openedx.core.djangoapps.self_paced.models import SelfPacedConfiguration
from openedx.core.djangoapps.user_api.preferences.api import set_user_preference
from openedx.core.djangoapps.waffle_utils.testutils import override_waffle_flag
from openedx.features.course_experience import UNIFIED_COURSE_TAB_FLAG
from student.tests.factories import CourseEnrollmentFactory, UserFactory, TEST_PASSWORD
from xmodule.modulestore.tests.django_utils import SharedModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory


@attr(shard=1)
@ddt.ddt
class CourseDateSummaryTest(SharedModuleStoreTestCase):
    """Tests for course date summary blocks."""

    def setUp(self):
        SelfPacedConfiguration(enable_course_home_improvements=True).save()
        super(CourseDateSummaryTest, self).setUp()

    def setup_course_and_user(
            self,
            days_till_start=1,
            days_till_end=14,
            days_till_upgrade_deadline=4,
            enroll_user=True,
            enrollment_mode=CourseMode.VERIFIED,
            user_enrollment_mode=None,
            course_min_price=100,
            days_till_verification_deadline=14,
            verification_status=None,
            sku=None,
            create_user=True
    ):
        """Set up the course and user for this test."""
        now = datetime.now(utc)

        # pylint: disable=attribute-defined-outside-init
        if create_user:
            self.user = UserFactory()

        self.course = CourseFactory.create(start=now + timedelta(days=days_till_start))

        if days_till_end is not None:
            self.course.end = now + timedelta(days=days_till_end)
        else:
            self.course.end = None

        if enrollment_mode is not None and days_till_upgrade_deadline is not None:
            CourseModeFactory.create(
                course_id=self.course.id,
                mode_slug=enrollment_mode,
                expiration_datetime=now + timedelta(days=days_till_upgrade_deadline),
                min_price=course_min_price,
                sku=sku
            )

        if enroll_user:
            if user_enrollment_mode:
                CourseEnrollmentFactory.create(course_id=self.course.id, user=self.user, mode=user_enrollment_mode)
            else:
                enrollment_mode = enrollment_mode or CourseMode.DEFAULT_MODE_SLUG
                CourseEnrollmentFactory.create(course_id=self.course.id, user=self.user, mode=enrollment_mode)

        if days_till_verification_deadline is not None:
            VerificationDeadline.objects.create(
                course_key=self.course.id,
                deadline=now + timedelta(days=days_till_verification_deadline)
            )

        if verification_status is not None:
            SoftwareSecurePhotoVerificationFactory.create(user=self.user, status=verification_status)

    def test_course_info_feature_flag(self):
        SelfPacedConfiguration(enable_course_home_improvements=False).save()
        self.setup_course_and_user()
        self.client.login(username=self.user.username, password=TEST_PASSWORD)
        url = reverse('info', args=(self.course.id,))
        response = self.client.get(url)
        self.assertNotIn('date-summary', response.content)

    def test_course_info_logged_out(self):
        self.setup_course_and_user()
        url = reverse('info', args=(self.course.id,))
        response = self.client.get(url)
        self.assertEqual(200, response.status_code)

    # Tests for which blocks are enabled
    def assert_block_types(self, expected_blocks):
        """Assert that the enabled block types for this course are as expected."""
        blocks = get_course_date_blocks(self.course, self.user)
        self.assertEqual(len(blocks), len(expected_blocks))
        self.assertEqual(set(type(b) for b in blocks), set(expected_blocks))

    @ddt.data(
        # Verified enrollment with no photo-verification before course start
        ({}, (CourseEndDate, CourseStartDate, TodaysDate, VerificationDeadlineDate)),
        # Verified enrollment with `approved` photo-verification after course end
        ({'days_till_start': -10,
          'days_till_end': -5,
          'days_till_upgrade_deadline': -6,
          'days_till_verification_deadline': -5,
          'verification_status': 'approved'},
         (TodaysDate, CourseEndDate)),
        # Verified enrollment with `expired` photo-verification during course run
        ({'days_till_start': -10,
          'verification_status': 'expired'},
         (TodaysDate, CourseEndDate, VerificationDeadlineDate)),
        # Verified enrollment with `approved` photo-verification during course run
        ({'days_till_start': -10,
          'verification_status': 'approved'},
         (TodaysDate, CourseEndDate)),
        # Audit enrollment and non-upsell course.
        ({'days_till_start': -10,
          'days_till_upgrade_deadline': None,
          'days_till_verification_deadline': None,
          'course_min_price': 0,
          'enrollment_mode': CourseMode.AUDIT},
         (TodaysDate, CourseEndDate)),
        # Verified enrollment with *NO* course end date
        ({'days_till_end': None},
         (CourseStartDate, TodaysDate, VerificationDeadlineDate)),
        # Verified enrollment with no photo-verification during course run
        ({'days_till_start': -1},
         (TodaysDate, CourseEndDate, VerificationDeadlineDate)),
        # Verification approved
        ({'days_till_start': -10,
          'days_till_upgrade_deadline': -1,
          'days_till_verification_deadline': 1,
          'verification_status': 'approved'},
         (TodaysDate, CourseEndDate)),
        # After upgrade deadline
        ({'days_till_start': -10,
          'days_till_upgrade_deadline': -1},
         (TodaysDate, CourseEndDate, VerificationDeadlineDate)),
        # After verification deadline
        ({'days_till_start': -10,
          'days_till_upgrade_deadline': -2,
          'days_till_verification_deadline': -1},
         (TodaysDate, CourseEndDate, VerificationDeadlineDate)),
        # Un-enrolled user before course start
        ({'enroll_user': False},
         (CourseStartDate, TodaysDate, CourseEndDate, VerifiedUpgradeDeadlineDate)),
        # Un-enrolled user during course run
        ({'days_till_start': -1,
          'enroll_user': False},
         (TodaysDate, CourseEndDate, VerifiedUpgradeDeadlineDate)),
        # Un-enrolled user after course end.
        ({'enroll_user': False,
          'days_till_start': -10,
          'days_till_end': -5},
         (TodaysDate, CourseEndDate, VerifiedUpgradeDeadlineDate)),
    )
    @ddt.unpack
    def test_enabled_block_types(self, course_options, expected_blocks):
        self.setup_course_and_user(**course_options)
        self.assert_block_types(expected_blocks)

    def test_todays_date_block(self):
        """
        Helper function to test that today's date block renders correctly
        and displays the correct time, accounting for daylight savings
        """
        with freeze_time('2015-01-02'):
            self.setup_course_and_user()
            block = TodaysDate(self.course, self.user)
            self.assertTrue(block.is_enabled)
            self.assertEqual(block.date, datetime.now(utc))
            self.assertEqual(block.title, 'current_datetime')

    @ddt.data(
        'info',
        'openedx.course_experience.course_home',
    )
    @override_waffle_flag(UNIFIED_COURSE_TAB_FLAG, active=True)
    def test_todays_date_no_timezone(self, url_name):
        with freeze_time('2015-01-02'):
            self.setup_course_and_user()
            self.client.login(username=self.user.username, password=TEST_PASSWORD)

            html_elements = [
                '<h3 class="hd hd-6 handouts-header">Important Course Dates</h3>',
                '<div class="date-summary-container">',
                '<div class="date-summary date-summary-todays-date">',
                '<span class="hd hd-6 heading localized-datetime"',
                'data-datetime="2015-01-02 00:00:00+00:00"',
                'data-string="Today is {date}"',
                'data-timezone="None"'
            ]
            url = reverse(url_name, args=(self.course.id,))
            response = self.client.get(url, follow=True)
            for html in html_elements:
                self.assertContains(response, html)

    @ddt.data(
        'info',
        'openedx.course_experience.course_home',
    )
    @override_waffle_flag(UNIFIED_COURSE_TAB_FLAG, active=True)
    def test_todays_date_timezone(self, url_name):
        with freeze_time('2015-01-02'):
            self.setup_course_and_user()
            self.client.login(username=self.user.username, password=TEST_PASSWORD)
            set_user_preference(self.user, "time_zone", "America/Los_Angeles")
            url = reverse(url_name, args=(self.course.id,))
            response = self.client.get(url, follow=True)

            html_elements = [
                '<h3 class="hd hd-6 handouts-header">Important Course Dates</h3>',
                '<div class="date-summary-container">',
                '<div class="date-summary date-summary-todays-date">',
                '<span class="hd hd-6 heading localized-datetime"',
                'data-datetime="2015-01-02 00:00:00+00:00"',
                'data-string="Today is {date}"',
                'data-timezone="America/Los_Angeles"'
            ]
            for html in html_elements:
                self.assertContains(response, html)

    ## Tests Course Start Date
    def test_course_start_date(self):
        self.setup_course_and_user()
        block = CourseStartDate(self.course, self.user)
        self.assertEqual(block.date, self.course.start)

    @ddt.data(
        'info',
        'openedx.course_experience.course_home',
    )
    @override_waffle_flag(UNIFIED_COURSE_TAB_FLAG, active=True)
    def test_start_date_render(self, url_name):
        with freeze_time('2015-01-02'):
            self.setup_course_and_user()
            self.client.login(username=self.user.username, password=TEST_PASSWORD)
            url = reverse(url_name, args=(self.course.id,))
            response = self.client.get(url, follow=True)
            html_elements = [
                'data-string="in 1 day - {date}"',
                'data-datetime="2015-01-03 00:00:00+00:00"'
            ]
            for html in html_elements:
                self.assertContains(response, html)

    @ddt.data(
        'info',
        'openedx.course_experience.course_home',
    )
    @override_waffle_flag(UNIFIED_COURSE_TAB_FLAG, active=True)
    def test_start_date_render_time_zone(self, url_name):
        with freeze_time('2015-01-02'):
            self.setup_course_and_user()
            self.client.login(username=self.user.username, password=TEST_PASSWORD)
            set_user_preference(self.user, "time_zone", "America/Los_Angeles")
            url = reverse(url_name, args=(self.course.id,))
            response = self.client.get(url, follow=True)
            html_elements = [
                'data-string="in 1 day - {date}"',
                'data-datetime="2015-01-03 00:00:00+00:00"',
                'data-timezone="America/Los_Angeles"'
            ]
            for html in html_elements:
                self.assertContains(response, html)

    ## Tests Course End Date Block
    def test_course_end_date_for_certificate_eligible_mode(self):
        self.setup_course_and_user(days_till_start=-1)
        block = CourseEndDate(self.course, self.user)
        self.assertEqual(
            block.description,
            'To earn a certificate, you must complete all requirements before this date.'
        )

    def test_course_end_date_for_non_certificate_eligible_mode(self):
        self.setup_course_and_user(days_till_start=-1, enrollment_mode=CourseMode.AUDIT)
        block = CourseEndDate(self.course, self.user)
        self.assertEqual(
            block.description,
            'After this date, course content will be archived.'
        )
        self.assertEqual(block.title, 'Course End')

    def test_course_end_date_after_course(self):
        self.setup_course_and_user(days_till_start=-2, days_till_end=-1)
        block = CourseEndDate(self.course, self.user)
        self.assertEqual(
            block.description,
            'This course is archived, which means you can review course content but it is no longer active.'
        )
        self.assertEqual(block.title, 'Course End')

    def test_ecommerce_checkout_redirect(self):
        """Verify the block link redirects to ecommerce checkout if it's enabled."""
        sku = 'TESTSKU'
        configuration = CommerceConfiguration.objects.create(checkout_on_ecommerce_service=True)
        self.setup_course_and_user(sku=sku)
        block = VerifiedUpgradeDeadlineDate(self.course, self.user)
        self.assertEqual(block.link, '{}?sku={}'.format(configuration.MULTIPLE_ITEMS_BASKET_PAGE_URL, sku))

    ## VerificationDeadlineDate
    def test_no_verification_deadline(self):
        self.setup_course_and_user(days_till_start=-1, days_till_verification_deadline=None)
        block = VerificationDeadlineDate(self.course, self.user)
        self.assertFalse(block.is_enabled)

    def test_no_verified_enrollment(self):
        self.setup_course_and_user(days_till_start=-1, enrollment_mode=CourseMode.AUDIT)
        block = VerificationDeadlineDate(self.course, self.user)
        self.assertFalse(block.is_enabled)

    def test_verification_deadline_date_upcoming(self):
        with freeze_time('2015-01-02'):
            self.setup_course_and_user(days_till_start=-1)
            block = VerificationDeadlineDate(self.course, self.user)
            self.assertEqual(block.css_class, 'verification-deadline-upcoming')
            self.assertEqual(block.title, 'Verification Deadline')
            self.assertEqual(block.date, datetime.now(utc) + timedelta(days=14))
            self.assertEqual(
                block.description,
                'You must successfully complete verification before this date to qualify for a Verified Certificate.'
            )
            self.assertEqual(block.link_text, 'Verify My Identity')
            self.assertEqual(block.link, reverse('verify_student_verify_now', args=(self.course.id,)))

    def test_verification_deadline_date_retry(self):
        with freeze_time('2015-01-02'):
            self.setup_course_and_user(days_till_start=-1, verification_status='denied')
            block = VerificationDeadlineDate(self.course, self.user)
            self.assertEqual(block.css_class, 'verification-deadline-retry')
            self.assertEqual(block.title, 'Verification Deadline')
            self.assertEqual(block.date, datetime.now(utc) + timedelta(days=14))
            self.assertEqual(
                block.description,
                'You must successfully complete verification before this date to qualify for a Verified Certificate.'
            )
            self.assertEqual(block.link_text, 'Retry Verification')
            self.assertEqual(block.link, reverse('verify_student_reverify'))

    def test_verification_deadline_date_denied(self):
        with freeze_time('2015-01-02'):
            self.setup_course_and_user(
                days_till_start=-10,
                verification_status='denied',
                days_till_verification_deadline=-1,
            )
            block = VerificationDeadlineDate(self.course, self.user)
            self.assertEqual(block.css_class, 'verification-deadline-passed')
            self.assertEqual(block.title, 'Missed Verification Deadline')
            self.assertEqual(block.date, datetime.now(utc) + timedelta(days=-1))
            self.assertEqual(
                block.description,
                "Unfortunately you missed this course's deadline for a successful verification."
            )
            self.assertEqual(block.link_text, 'Learn More')
            self.assertEqual(block.link, '')

    @ddt.data(
        (-1, '1 day ago - {date}'),
        (1, 'in 1 day - {date}')
    )
    @ddt.unpack
    def test_render_date_string_past(self, delta, expected_date_string):
        with freeze_time('2015-01-02'):
            self.setup_course_and_user(
                days_till_start=-10,
                verification_status='denied',
                days_till_verification_deadline=delta,
            )
            block = VerificationDeadlineDate(self.course, self.user)
            self.assertEqual(block.relative_datestring, expected_date_string)

    def create_self_paced_course_run(self, **kwargs):
        defaults = {
            'enroll_user': False,
            'days_till_upgrade_deadline': 100,
        }
        defaults.update(kwargs)
        self.setup_course_and_user(**defaults)
        self.course.self_paced = True
        self.store.update_item(self.course, self.user.id)
        overview = CourseOverview.get_from_id(self.course.id)
        self.assertTrue(overview.self_paced)

    def test_date_with_self_paced(self):
        """ The date returned for self-paced course runs should be dependent on the learner's enrollment date. """
        global_config = DynamicUpgradeDeadlineConfiguration.objects.create(enabled=True)

        # Enrollments made before the course start should use the course start date as the content availability date
        self.create_self_paced_course_run(days_till_start=3)
        CourseEnrollmentFactory.create(course_id=self.course.id, user=self.user, mode=CourseMode.AUDIT)
        block = VerifiedUpgradeDeadlineDate(self.course, self.user)
        overview = CourseOverview.get_from_id(self.course.id)
        expected = overview.start + timedelta(days=global_config.deadline_days)
        self.assertEqual(block.date, expected)

        # Enrollments made after the course start should use the enrollment date as the content availability date
        self.create_self_paced_course_run(days_till_start=-1)
        enrollment = CourseEnrollmentFactory.create(course_id=self.course.id, user=self.user, mode=CourseMode.AUDIT)
        block = VerifiedUpgradeDeadlineDate(self.course, self.user)
        expected = enrollment.created + timedelta(days=global_config.deadline_days)
        self.assertEqual(block.date, expected)

        # Courses should be able to override the deadline
        course_config = CourseDynamicUpgradeDeadlineConfiguration.objects.create(
            enabled=True, course_id=self.course.id, opt_out=False, deadline_days=3
        )
        block = VerifiedUpgradeDeadlineDate(self.course, self.user)
        expected = enrollment.created + timedelta(days=course_config.deadline_days)
        self.assertEqual(block.date, expected)

        # Disabling the functionality should result in the verified mode's expiration date being returned.
        global_config.enabled = False
        global_config.save()
        block = VerifiedUpgradeDeadlineDate(self.course, self.user)
        expected = CourseMode.objects.get(course_id=self.course.id, mode_slug=CourseMode.VERIFIED).expiration_datetime
        self.assertEqual(block.date, expected)

    def test_date_with_self_paced_with_course_opt_out(self):
        """ If the course run has opted out of the dynamic deadline, the course mode's deadline should be used. """
        self.create_self_paced_course_run(days_till_start=-1)
        DynamicUpgradeDeadlineConfiguration.objects.create(enabled=True)
        CourseEnrollmentFactory.create(course_id=self.course.id, user=self.user, mode=CourseMode.AUDIT)

        # Opt the course out of the dynamic upgrade deadline
        CourseDynamicUpgradeDeadlineConfiguration.objects.create(enabled=True, course_id=self.course.id, opt_out=True)

        block = VerifiedUpgradeDeadlineDate(self.course, self.user)
        expected = CourseMode.objects.get(course_id=self.course.id, mode_slug=CourseMode.VERIFIED).expiration_datetime
        self.assertEqual(block.date, expected)
