from datetime import datetime

from django.conf import settings
from django.contrib.auth.models import User
from django.test.utils import override_settings

from elasticsearch.exceptions import NotFoundError
from mock import MagicMock, Mock, call, patch
from nose.tools import eq_, ok_

from mozillians.common.tests import TestCase
from mozillians.users.managers import PUBLIC
from mozillians.users.models import UserProfile
from mozillians.users.tasks import (_email_basket_managers, index_objects,
                                    remove_incomplete_accounts, unindex_objects,
                                    unsubscribe_from_basket_task)
from mozillians.users.tests import UserFactory


class IncompleteAccountsTests(TestCase):
    """Incomplete accounts removal tests."""

    @patch('mozillians.users.tasks.datetime')
    def test_remove_incomplete_accounts(self, datetime_mock):
        """Test remove incomplete accounts."""
        complete_user = UserFactory.create(vouched=False,
                                           date_joined=datetime(2012, 01, 01))
        complete_vouched_user = UserFactory.create(date_joined=datetime(2013, 01, 01))
        incomplete_user_not_old = UserFactory.create(date_joined=datetime(2013, 01, 01),
                                                     userprofile={'full_name': ''})
        incomplete_user_old = UserFactory.create(date_joined=datetime(2012, 01, 01),
                                                 userprofile={'full_name': ''})

        datetime_mock.now.return_value = datetime(2013, 01, 01)

        remove_incomplete_accounts(days=0)
        ok_(User.objects.filter(id=complete_user.id).exists())
        ok_(User.objects.filter(id=complete_vouched_user.id).exists())
        ok_(User.objects.filter(id=incomplete_user_not_old.id).exists())
        ok_(not User.objects.filter(id=incomplete_user_old.id).exists())


@override_settings(ES_DISABLED=False)
class ElasticSearchIndexTests(TestCase):
    @patch('mozillians.users.tasks.get_es')
    def test_index_objects(self, get_es_mock):
        user_1 = UserFactory.create()
        user_2 = UserFactory.create()
        mapping_type = MagicMock()
        model = MagicMock()
        mapping_type.get_model.return_value = model
        model.objects.filter.return_value = [user_1.userprofile,
                                             user_2.userprofile]
        mapping_type.extract_document.return_value = 'foo'
        index_objects(mapping_type,
                      [user_1.userprofile.id, user_2.userprofile.id],
                      public_index=False)
        mapping_type.bulk_index.assert_has_calls([
            call(['foo', 'foo'], id_field='id', es=get_es_mock(),
                 index=mapping_type.get_index(False))])

    @patch('mozillians.users.tasks.get_es')
    def test_index_objects_public(self, get_es_mock):
        user_1 = UserFactory.create()
        user_2 = UserFactory.create()
        mapping_type = MagicMock()
        model = MagicMock()
        mapping_type.get_model.return_value = model
        qs = model.objects.filter().public_indexable().privacy_level
        qs.return_value = [user_1.userprofile, user_2.userprofile]
        mapping_type.extract_document.return_value = 'foo'
        index_objects(mapping_type,
                      [user_1.userprofile.id, user_2.userprofile.id],
                      public_index=True)

        model.objects.assert_has_calls([
            call.filter(id__in=(user_1.userprofile.id, user_2.userprofile.id)),
            call.filter().public_indexable(),
            call.filter().public_indexable().privacy_level(PUBLIC),
        ])
        mapping_type.bulk_index.assert_has_calls([
            call(['foo', 'foo'], id_field='id', es=get_es_mock(),
                 index=mapping_type.get_index(True))])

    @patch('mozillians.users.tasks.get_es')
    def test_unindex_objects(self, get_es_mock):
        mapping_type = MagicMock()
        unindex_objects(mapping_type, [1, 2, 3], 'foo')
        ok_(mapping_type.unindex.called)
        mapping_type.assert_has_calls([
            call.unindex(1, es=get_es_mock(), public_index='foo'),
            call.unindex(2, es=get_es_mock(), public_index='foo'),
            call.unindex(3, es=get_es_mock(), public_index='foo')])

    def test_unindex_raises_not_found_exception(self):
        exception = NotFoundError(404, {'not found': 'not found '}, {'foo': 'foo'})
        mapping_type = Mock()
        mapping_type.unindex(side_effect=exception)
        unindex_objects(mapping_type, [1, 2, 3], 'foo')


class BasketTests(TestCase):
    @override_settings(BASKET_MANAGERS=False)
    @patch('mozillians.users.tasks.send_mail')
    def test_email_basket_managers_email_not_set(self, send_mail_mock):
        _email_basket_managers('foo', 'bar', 'error')
        ok_(not send_mail_mock.called)

    @override_settings(BASKET_MANAGERS='basket_managers',
                       FROM_NOREPLY='noreply')
    @patch('mozillians.users.tasks.send_mail')
    def test_email_basket_managers(self, send_mail_mock):
        subject = '[Mozillians - ET] Failed to subscribe or update user bar'
        body = """
    Something terrible happened while trying to subscribe user bar from Basket.

    Here is the error message:

    error
    """
        _email_basket_managers('subscribe', 'bar', 'error')
        send_mail_mock.assert_called_with(
            subject, body, 'noreply', 'basket_managers', fail_silently=False)

    @override_settings(BASKET_NEWSLETTER='newsletter')
    @patch('mozillians.users.tasks.BASKET_ENABLED', True)
    @patch('mozillians.users.tasks.basket')
    def test_change_email(self, mock_basket):
        # When a user's email is changed, their old email is unsubscribed
        # from all newsletters and their new email is subscribed to them.

        # Create a new user
        email = 'foo@example.com'
        token = 'first token'
        mock_basket.lookup_user.return_value = {
            'email': email,  # the old value
            'token': token,
            'newsletters': ['foo', 'bar']
        }
        mock_basket.subscribe.return_value = {
            'token': token,
        }
        user = UserFactory.create(email=email)
        up = UserProfile.objects.get(pk=user.userprofile.pk)
        eq_(token, up.basket_token)

        new_email = 'bar@example.com'
        new_token = 'NEW token'
        mock_basket.subscribe.return_value = {
            'token': new_token,
        }
        user.email = new_email
        user.save()
        mock_basket.lookup_user.assert_called_with(token=token)
        mock_basket.unsubscribe.assert_called_with(
            token=token, email=email, optout='Y', newsletters=[settings.BASKET_NEWSLETTER]
        )
        mock_basket.subscribe.assert_called_with(
            new_email,
            [settings.BASKET_NEWSLETTER],
            trigger_welcome='N',
            sync='Y'
        )
        up = UserProfile.objects.get(pk=user.userprofile.pk)
        eq_(new_token, up.basket_token)

    @override_settings(BASKET_NEWSLETTER='newsletter')
    @patch('mozillians.users.tasks.basket.unsubscribe')
    def test_unsubscribe_from_basket_task(self, unsubscribe_mock):
        user = UserFactory.create(userprofile={'basket_token': 'foo'})
        with patch('mozillians.users.tasks.BASKET_ENABLED', True):
            unsubscribe_from_basket_task(user.email, user.userprofile.basket_token)
        unsubscribe_mock.assert_called_with(
            user.userprofile.basket_token, user.email, newsletters='newsletter')

    @override_settings(BASKET_NEWSLETTER='newsletter')
    @patch('mozillians.users.tasks.basket')
    @patch.object(UserProfile, 'lookup_basket_token')
    def test_unsubscribe_from_basket_task_without_token(self, lookup_token_mock, basket_mock):
        lookup_token_mock.return_value = 'basket_token'
        basket_mock.lookup_user.return_value = {'token': 'basket_token'}
        user = UserFactory.create(userprofile={'basket_token': ''})
        with patch('mozillians.users.tasks.BASKET_ENABLED', True):
            unsubscribe_from_basket_task(user.email, user.userprofile.basket_token)
        user = User.objects.get(pk=user.pk)  # refresh data from DB
        basket_mock.unsubscribe.assert_called_with(
            'basket_token', user.email, newsletters='newsletter')
